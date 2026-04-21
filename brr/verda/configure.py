"""Interactive configuration wizard for the Verda provider."""

import os
import stat
import subprocess
from datetime import datetime
from pathlib import Path

import click
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.console import Console
from rich.panel import Panel

from brr.state import ensure_state_dirs, read_config, write_config, CONFIG_PATH, KEYS_DIR
from brr.verda.nodes import (
    VERDA_CONFIG_PATH,
    VERDA_CREDENTIALS_PATH,
    _verda_client,
    _verda_credentials,
)


console = Console()


def _check_credentials():
    """Verify Verda credentials are available. Returns True on success."""
    env_ok = os.environ.get("VERDA_CLIENT_ID") and os.environ.get("VERDA_CLIENT_SECRET")
    if env_ok:
        console.print("Verda auth: [green]VERDA_CLIENT_ID/VERDA_CLIENT_SECRET env vars[/green]")
        return True
    if VERDA_CREDENTIALS_PATH.exists():
        console.print(f"Verda credentials: [green]{VERDA_CREDENTIALS_PATH}[/green]")
        if VERDA_CONFIG_PATH.exists():
            console.print(f"Verda profile: [green]{VERDA_CONFIG_PATH}[/green]")
        return True

    console.print("[red]No Verda credentials found[/red]")
    console.print("  Run [bold]verda auth login[/bold] to authenticate")
    console.print("  Or set [bold]VERDA_CLIENT_ID[/bold] and [bold]VERDA_CLIENT_SECRET[/bold]")
    return False


def _list_locations(client):
    try:
        return client.locations.get() or []
    except Exception as e:
        console.print(f"[yellow]Warning: failed to list Verda locations: {e}[/yellow]")
        return []


def _prompt_location(client, prompt, default="FIN-01"):
    """Prompt the user for a Verda location code. Uses the API list if available."""
    locations = _list_locations(client)
    if locations:
        choices = []
        for loc in locations:
            code = loc.get("code") if isinstance(loc, dict) else getattr(loc, "code", "")
            name = loc.get("name", code) if isinstance(loc, dict) else getattr(loc, "name", code)
            if not code:
                continue
            choices.append(Choice(value=code, name=f"{code} — {name}"))
        known_codes = {c.value for c in choices}
        picked_default = default if default in known_codes else (choices[0].value if choices else default)
        return inquirer.select(
            message=prompt,
            choices=choices,
            default=picked_default,
        ).execute()
    return click.prompt(prompt, default=default)


def _existing_local_ssh_key():
    """Return the most recent brr-managed Verda SSH key path, or None."""
    candidates = sorted(
        f for f in os.listdir(KEYS_DIR)
        if f.startswith("verda-") and not f.endswith(".pub")
    )
    if candidates:
        return str(KEYS_DIR / candidates[0])
    return None


def _generate_local_ssh_key():
    """Create a new ed25519 keypair under KEYS_DIR. Returns private key path."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    key_name = f"verda-{timestamp}"
    key_path = str(KEYS_DIR / key_name)
    console.print(f"Generating SSH key: [bold cyan]{key_name}[/bold cyan]...")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path, "-C", "brr-verda"],
        check=True, capture_output=True,
    )
    os.chmod(key_path, stat.S_IRUSR)
    console.print(f"SSH key: [green]{key_path}[/green]")
    console.print(f"Public key: [green]{key_path}.pub[/green]")
    return key_path


def _read_public_key(ssh_key_path):
    pub_path = Path(f"{ssh_key_path}.pub")
    if pub_path.exists():
        return pub_path.read_text().strip()
    result = subprocess.run(
        ["ssh-keygen", "-y", "-f", ssh_key_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to derive public key: {result.stderr.strip()}")
    return result.stdout.strip()


def _public_key_body(public_key):
    """Extract the 'type base64' portion of an OpenSSH public key for matching."""
    parts = public_key.strip().split(None, 2)
    if len(parts) < 2:
        return public_key.strip()
    return f"{parts[0]} {parts[1]}"


def _register_ssh_key(client, public_key, desired_name):
    """Register the SSH key with Verda (or reuse a matching one). Returns key id."""
    target_body = _public_key_body(public_key)
    for key in client.ssh_keys.get():
        existing = getattr(key, "public_key", "") or ""
        if _public_key_body(existing) == target_body:
            console.print(f"Reusing existing Verda SSH key: [green]{key.name}[/green]")
            return key.id
    created = client.ssh_keys.create(name=desired_name, key=public_key)
    console.print(f"Registered Verda SSH key: [green]{created.name}[/green]")
    return created.id


def _list_shared_volumes(client):
    try:
        vols = client.volumes.get() or []
    except Exception as e:
        console.print(f"[yellow]Warning: failed to list Verda volumes: {e}[/yellow]")
        return []
    return [
        v for v in vols
        if "shared" in (getattr(v, "type", "") or "").lower()
        and not getattr(v, "is_os_volume", False)
    ]


def _mount_target_from_volume(volume):
    """Extract an fstab-compatible mount target from a Verda volume object.

    Returns a string suitable for ``mount`` (``host:/path`` or ``LABEL=...``)
    or an empty string if we can't tell. setup.sh falls back to runtime
    discovery when this is empty.
    """
    for attr in ("mount_command", "pseudo_path", "target", "filesystem_to_fstab_command"):
        value = getattr(volume, attr, None)
        if not value:
            continue
        text = str(value).strip()
        # Pull the first token that looks like a mount source.
        for token in text.split():
            if ":/" in token:
                return token
            if token.startswith("LABEL=") or token.startswith("UUID=") or token.startswith("/dev/"):
                return token
    return ""


def _create_shared_volume(client, name, size_gb, location):
    try:
        return client.volumes.create(
            type="NVMe_Shared",
            name=name,
            size=size_gb,
            location=location,
        )
    except Exception as e:
        console.print(f"[red]Failed to create shared volume: {e}[/red]")
        return None




def configure_verda():
    """Interactive Verda configuration wizard."""
    ensure_state_dirs()
    existing = read_config()

    console.print(Panel("Verda configuration", title="brr configure", border_style="cyan"))

    # --- Credentials ---
    if not _check_credentials():
        raise click.Abort()
    console.print()

    try:
        client = _verda_client()
        # Prime credentials by touching the SDK. `_verda_credentials` raises if
        # neither env vars nor the credentials file resolve.
        _verda_credentials()
    except Exception as e:
        console.print(f"[red]Failed to initialize Verda client: {e}[/red]")
        raise click.Abort() from e

    # --- SSH key ---
    ssh_key = existing.get("VERDA_SSH_KEY", "")
    if ssh_key and Path(ssh_key).exists():
        console.print(f"\nUsing existing SSH key: [green]{ssh_key}[/green]")
        if click.confirm("Generate a new key instead?", default=False):
            ssh_key = _generate_local_ssh_key()
    else:
        local = _existing_local_ssh_key()
        if local:
            ssh_key = local
            console.print(f"\nUsing existing SSH key: [green]{ssh_key}[/green]")
        else:
            console.print()
            ssh_key = _generate_local_ssh_key()

    public_key = _read_public_key(ssh_key)
    import socket
    key_label = f"brr-verda-{socket.gethostname()}"
    try:
        ssh_key_id = _register_ssh_key(client, public_key, key_label)
    except Exception as e:
        console.print(f"[red]Failed to register SSH key with Verda: {e}[/red]")
        raise click.Abort() from e

    # --- Shared filesystem ---
    console.print()
    shared_volume_id = existing.get("VERDA_SHARED_VOLUME_ID", "")
    shared_mount_target = existing.get("VERDA_SHARED_MOUNT_TARGET", "")
    if click.confirm(
        "Set up shared filesystem for persistent ~/code?",
        default=bool(shared_volume_id),
    ):
        volumes = _list_shared_volumes(client)
        vol_choices = []
        for v in volumes:
            vol_choices.append(
                Choice(value=v.id, name=f"{v.name} ({v.id}) — {v.size} GB {v.type}")
            )
        vol_choices.append(Choice(value="_create", name="Create new shared volume"))

        default_vol = shared_volume_id if shared_volume_id in {v.id for v in volumes} else None
        chosen = inquirer.select(
            message="Select shared volume",
            choices=vol_choices,
            default=default_vol,
        ).execute()

        if chosen == "_create":
            name = click.prompt("Volume name", default="brr-shared")
            size_gb = click.prompt("Size (GB)", default=100, type=int)
            volume_location = _prompt_location(
                client,
                prompt="Create shared volume in which datacenter?",
                default="FIN-01",
            )
            with console.status("[bold green]Creating shared volume..."):
                new_vol = _create_shared_volume(client, name, size_gb, volume_location)
            if new_vol is None:
                shared_volume_id = ""
            else:
                shared_volume_id = new_vol.id
                shared_mount_target = _mount_target_from_volume(new_vol)
                console.print(
                    f"Created shared volume: [green]{shared_volume_id}[/green] "
                    f"in [green]{volume_location}[/green]"
                )
                console.print(
                    f"[yellow]Note:[/yellow] only launch clusters in [bold]{volume_location}[/bold] "
                    f"to use this shared volume."
                )
        else:
            shared_volume_id = chosen
            match = next((v for v in volumes if v.id == chosen), None)
            if match is not None:
                shared_mount_target = _mount_target_from_volume(match)

        if shared_volume_id and not shared_mount_target:
            console.print(
                "[yellow]Could not determine shared volume mount target — "
                "setup.sh will discover it at runtime.[/yellow]"
            )
    else:
        shared_volume_id = ""
        shared_mount_target = ""

    # --- GitHub SSH ---
    console.print()
    github_ssh_key = existing.get("GITHUB_SSH_KEY", "")
    if click.confirm(
        "Set up GitHub SSH access for clusters?",
        default=bool(github_ssh_key),
    ):
        from brr.github import ensure_github_key
        github_ssh_key = ensure_github_key(existing)
    else:
        github_ssh_key = ""

    # --- Write config ---
    updates = {
        "VERDA_SSH_KEY": ssh_key,
        "VERDA_SSH_KEY_ID": ssh_key_id,
        "VERDA_SHARED_VOLUME_ID": shared_volume_id,
        "VERDA_SHARED_MOUNT_TARGET": shared_mount_target,
        "VERDA_GPU_IMAGE": existing.get("VERDA_GPU_IMAGE", "ubuntu-24.04-cuda-12.8-open-docker"),
        "VERDA_CPU_IMAGE": existing.get("VERDA_CPU_IMAGE", "ubuntu-24.04-cuda-12.8-open-docker"),
        "GITHUB_SSH_KEY": github_ssh_key,
    }

    merged = dict(existing)
    merged.update(updates)
    write_config(merged)
    console.print(f"\nWrote [green]{CONFIG_PATH}[/green]")

    console.print()
    console.print("[bold green]Done![/bold green] Next steps:")
    console.print("  brr configure tools                         # select AI coding tools")
    console.print("  brr configure general                       # instance settings")
    console.print("  brr up verda:h100                           # launch 1xH100 GPU node")
    console.print("  brr up verda:cpu                            # launch CPU cluster")
