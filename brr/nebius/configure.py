import os
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import click
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.console import Console
from rich.panel import Panel

from brr.state import ensure_state_dirs, read_config, write_config, CONFIG_PATH, KEYS_DIR

console = Console()

NEBIUS_CREDS_PATH = Path.home() / ".nebius" / "credentials.json"

DEFAULTS = {
    "NEBIUS_FILESYSTEM_ID": "",
}


def _check_credentials():
    """Verify Nebius credentials exist."""
    if NEBIUS_CREDS_PATH.exists():
        console.print(f"Nebius credentials: [green]{NEBIUS_CREDS_PATH}[/green]")
        return True
    if os.environ.get("NEBIUS_IAM_TOKEN"):
        console.print("Nebius auth via [green]NEBIUS_IAM_TOKEN[/green] env var")
        return True

    console.print("[red]No Nebius credentials found[/red]")
    console.print(f"  Expected: {NEBIUS_CREDS_PATH}")
    console.print("  Create a service account key at https://console.nebius.com/")
    console.print("  Then save it as ~/.nebius/credentials.json")
    return False


def _nebius_sdk():
    """Create a Nebius SDK instance with credentials discovery."""
    from nebius.sdk import SDK

    if NEBIUS_CREDS_PATH.exists():
        return SDK(credentials_file_name=str(NEBIUS_CREDS_PATH))
    return SDK()


def _list_subnets(project_id):
    """Try to list subnets via Nebius SDK. Returns list of (id, name, zone) or None."""
    try:
        import asyncio
        from nebius.api.nebius.vpc.v1 import SubnetServiceClient, ListSubnetsRequest

        async def _list():
            sdk = _nebius_sdk()
            async with sdk:
                client = SubnetServiceClient(sdk)
                resp = await client.list(ListSubnetsRequest(parent_id=project_id))
                subnets = []
                for s in resp.items:
                    name = s.metadata.name if s.metadata.name else "(unnamed)"
                    zone = ""
                    if hasattr(s.spec, "zone"):
                        zone = s.spec.zone
                    subnets.append((s.metadata.id, name, zone))
                return subnets

        return asyncio.run(_list())
    except Exception as e:
        Console(stderr=True).print(f"[yellow]Warning:[/yellow] Failed to list subnets: {e}")
        return None


def _list_filesystems(project_id):
    """List existing shared filesystems. Returns list of (id, name, size_gb) or None."""
    try:
        import asyncio
        from nebius.api.nebius.compute.v1 import FilesystemServiceClient, ListFilesystemsRequest

        async def _list():
            sdk = _nebius_sdk()
            async with sdk:
                client = FilesystemServiceClient(sdk)
                resp = await client.list(ListFilesystemsRequest(parent_id=project_id))
                filesystems = []
                for fs in resp.items:
                    name = fs.metadata.name if fs.metadata.name else "(unnamed)"
                    size_gb = getattr(fs.spec, "size_gibibytes", 0) or 0
                    filesystems.append((fs.metadata.id, name, size_gb))
                return filesystems

        return asyncio.run(_list())
    except Exception as e:
        Console(stderr=True).print(f"[yellow]Warning:[/yellow] Failed to list filesystems: {e}")
        return None


def _create_filesystem(project_id, name, size_gb):
    """Create a shared filesystem. Returns filesystem ID or None."""
    try:
        import asyncio
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.compute.v1 import (
            FilesystemServiceClient,
            CreateFilesystemRequest,
            FilesystemSpec,
        )

        async def _create():
            sdk = _nebius_sdk()
            async with sdk:
                client = FilesystemServiceClient(sdk)
                op = await client.create(CreateFilesystemRequest(
                    metadata=ResourceMetadata(
                        parent_id=project_id,
                        name=name,
                    ),
                    spec=FilesystemSpec(
                        type=FilesystemSpec.FilesystemType.NETWORK_SSD,
                        size_gibibytes=size_gb,
                    ),
                ))
                await op.wait()
                return op.resource_id

        return asyncio.run(_create())
    except Exception as e:
        console.print(f"[red]Failed to create filesystem: {e}[/red]")
        return None


def _get_or_create_ssh_key():
    """Find an existing Nebius SSH key or generate a new one."""
    ensure_state_dirs()

    # Check for existing Nebius keys
    local_keys = sorted(
        f for f in os.listdir(KEYS_DIR)
        if f.startswith("nebius-") and not f.endswith(".pub")
    )
    if local_keys:
        key_path = str(KEYS_DIR / local_keys[0])
        console.print(f"Using existing SSH key: [green]{local_keys[0]}[/green]")
        return key_path

    # Generate new keypair
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    key_name = f"nebius-{timestamp}"
    key_path = str(KEYS_DIR / key_name)

    console.print(f"Generating SSH key: [bold cyan]{key_name}[/bold cyan]...")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path, "-C", "brr-nebius"],
        check=True, capture_output=True,
    )
    os.chmod(key_path, stat.S_IRUSR)

    console.print(f"SSH key: [green]{key_path}[/green]")
    console.print(f"Public key: [green]{key_path}.pub[/green]")
    return key_path


def _setup_github_ssh(ssh_key):
    """Add SSH public key to GitHub and return the private key path for file_mounts.

    Returns the private key path (for GITHUB_SSH_KEY config), or empty string on failure.
    """
    # Derive public key
    pubkey_result = subprocess.run(
        ["ssh-keygen", "-y", "-f", ssh_key], capture_output=True, text=True
    )
    if pubkey_result.returncode != 0:
        console.print(f"[red]Failed to derive public key: {pubkey_result.stderr.strip()}[/red]")
        return ssh_key  # Still return path so key is copied for GitHub access

    if not shutil.which("gh"):
        console.print("[yellow]gh CLI not found — skipping GitHub key registration[/yellow]")
        console.print("[yellow]Install gh and run 'brr configure nebius' again to add the key[/yellow]")
        return ssh_key

    auth_check = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True
    )
    if auth_check.returncode != 0:
        console.print("[yellow]gh CLI not authenticated — skipping GitHub key registration[/yellow]")
        console.print("[yellow]Run 'gh auth login' and then 'brr configure nebius' again[/yellow]")
        return ssh_key

    # Check if key already registered
    list_result = subprocess.run(
        ["gh", "ssh-key", "list"], capture_output=True, text=True
    )
    if list_result.returncode == 0:
        for line in list_result.stdout.splitlines():
            if "brr-nebius" in line:
                console.print("SSH key already registered on GitHub: [green]brr-nebius[/green]")
                return ssh_key

    # Add public key to GitHub
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False) as tmp:
        tmp.write(pubkey_result.stdout)
        tmp_path = tmp.name

    try:
        add_result = subprocess.run(
            ["gh", "ssh-key", "add", tmp_path, "--title", "brr-nebius"],
            capture_output=True, text=True,
        )
        if add_result.returncode == 0:
            console.print("Added SSH key to GitHub: [green]brr-nebius[/green]")
        else:
            console.print(f"[red]Failed to add key to GitHub: {add_result.stderr.strip()}[/red]")
    finally:
        os.unlink(tmp_path)

    return ssh_key


def configure_nebius():
    """Interactive Nebius configuration wizard."""
    ensure_state_dirs()
    existing = read_config()

    console.print(Panel("Nebius configuration", title="brr configure", border_style="cyan"))

    # --- Credentials ---
    if not _check_credentials():
        raise click.Abort()
    console.print()

    # --- Project ID ---
    project_id = click.prompt(
        "Nebius project ID",
        default=existing.get("NEBIUS_PROJECT_ID", ""),
    )

    # --- Subnet ---
    subnets = _list_subnets(project_id)
    if subnets:
        default_subnet = existing.get("NEBIUS_SUBNET_ID", subnets[0][0])
        subnet_choices = []
        for sid, name, zone in subnets:
            label = f"{name} ({sid})"
            if zone:
                label += f" — {zone}"
            subnet_choices.append(Choice(value=sid, name=label))

        subnet_id = inquirer.select(
            message="Select subnet",
            choices=subnet_choices,
            default=default_subnet if default_subnet in [s[0] for s in subnets] else None,
        ).execute()
    else:
        subnet_id = click.prompt(
            "Nebius subnet ID",
            default=existing.get("NEBIUS_SUBNET_ID", ""),
        )

    # --- SSH key ---
    ssh_key = existing.get("NEBIUS_SSH_KEY", "")
    if ssh_key and Path(ssh_key).exists():
        console.print(f"\nUsing existing SSH key: [green]{ssh_key}[/green]")
        if click.confirm("Generate a new key instead?", default=False):
            ssh_key = _get_or_create_ssh_key()
    else:
        console.print()
        ssh_key = _get_or_create_ssh_key()

    # --- Shared filesystem ---
    console.print()
    filesystem_id = existing.get("NEBIUS_FILESYSTEM_ID", "")
    if click.confirm(
        "Set up shared filesystem for persistent ~/code?",
        default=bool(filesystem_id),
    ):
        filesystems = _list_filesystems(project_id)
        fs_choices = []
        if filesystems:
            for fid, name, size_gb in filesystems:
                fs_choices.append(Choice(value=fid, name=f"{name} ({fid}) — {size_gb} GB"))
        fs_choices.append(Choice(value="_create", name="Create new filesystem"))

        default_fs = filesystem_id if filesystem_id in [f[0] for f in (filesystems or [])] else None
        choice = inquirer.select(
            message="Select filesystem",
            choices=fs_choices,
            default=default_fs,
        ).execute()

        if choice == "_create":
            fs_name = click.prompt("Filesystem name", default="brr-shared")
            fs_size = click.prompt("Size (GB)", default=100, type=int)
            with console.status("[bold green]Creating filesystem..."):
                filesystem_id = _create_filesystem(project_id, fs_name, fs_size) or ""
            if filesystem_id:
                console.print(f"Created filesystem: [green]{filesystem_id}[/green]")
        else:
            filesystem_id = choice
    else:
        filesystem_id = ""

    # --- GitHub SSH access ---
    console.print()
    github_ssh_key = existing.get("GITHUB_SSH_KEY", "")
    if click.confirm(
        "Set up GitHub SSH access for clusters?",
        default=bool(github_ssh_key),
    ):
        github_ssh_key = _setup_github_ssh(ssh_key)
    else:
        github_ssh_key = ""

    # --- Write config (merge with existing) ---
    updates = {
        "NEBIUS_PROJECT_ID": project_id,
        "NEBIUS_SUBNET_ID": subnet_id,
        "NEBIUS_SSH_KEY": ssh_key,
        "NEBIUS_FILESYSTEM_ID": filesystem_id,
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
    console.print("  brr up nebius:h100                          # launch H100 GPU cluster")
