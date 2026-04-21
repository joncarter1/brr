"""Dedicated brr GitHub SSH key management.

Maintains a single ed25519 key under ``~/.brr/keys/github-*`` that's
independent of any provider's cluster SSH key. All providers share this
one key at cluster boot (via ``GITHUB_SSH_KEY`` → ``staging/github_key`` →
``~/.ssh/github_key``).

Rationale: previously each provider wizard registered its own cluster SSH
key with GitHub and overwrote ``GITHUB_SSH_KEY``, so whichever provider
was configured last "won". Rotating a cluster key then silently broke
GitHub access on other clusters, and a leaked cluster SSH key was also a
push-access GitHub key. Separate dedicated key decouples those concerns.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from rich.console import Console

from brr.state import KEYS_DIR, ensure_state_dirs


console = Console()


def _hostname():
    return socket.gethostname().split(".")[0] or "host"


GITHUB_KEY_TITLE = f"brr-{_hostname()}"


def _existing_brr_github_key():
    """Return the most recent ``~/.brr/keys/github-*`` private key path, or None."""
    if not KEYS_DIR.exists():
        return None
    candidates = sorted(
        f for f in os.listdir(KEYS_DIR)
        if f.startswith("github-") and not f.endswith(".pub")
    )
    if candidates:
        return str(KEYS_DIR / candidates[-1])
    return None


def _generate_github_key():
    """Generate a new ed25519 keypair at ``~/.brr/keys/github-<timestamp>``."""
    ensure_state_dirs()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"github-{ts}"
    path = str(KEYS_DIR / name)
    console.print(f"Generating GitHub SSH key: [bold cyan]{name}[/bold cyan]")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", path, "-C", GITHUB_KEY_TITLE],
        check=True, capture_output=True,
    )
    os.chmod(path, stat.S_IRUSR)
    return path


def _read_public_key(private_path):
    pub = Path(f"{private_path}.pub")
    if pub.exists():
        return pub.read_text().strip()
    result = subprocess.run(
        ["ssh-keygen", "-y", "-f", private_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to derive public key: {result.stderr.strip()}")
    return result.stdout.strip()


def _pub_key_body(public_key):
    """Strip the comment from an OpenSSH public key, return ``type base64``."""
    parts = public_key.strip().split(None, 2)
    if len(parts) < 2:
        return public_key.strip()
    return f"{parts[0]} {parts[1]}"


def _gh_authenticated():
    if not shutil.which("gh"):
        console.print("[yellow]gh CLI not found — skipping GitHub key registration[/yellow]")
        console.print("[yellow]Install gh, then re-run `brr configure`[/yellow]")
        return False
    auth_check = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if auth_check.returncode != 0:
        console.print("[yellow]gh CLI not authenticated — skipping GitHub key registration[/yellow]")
        console.print("[yellow]Run `gh auth login`, then re-run `brr configure`[/yellow]")
        return False
    return True


def _register_with_github(public_key):
    """Register a public key with GitHub, or reuse a match by pubkey body."""
    if not _gh_authenticated():
        return False

    body = _pub_key_body(public_key)
    list_result = subprocess.run(
        ["gh", "api", "user/keys"], capture_output=True, text=True,
    )
    if list_result.returncode == 0:
        try:
            for k in json.loads(list_result.stdout):
                if _pub_key_body(k.get("key", "")) == body:
                    console.print(
                        f"SSH key already registered on GitHub: "
                        f"[green]{k.get('title', GITHUB_KEY_TITLE)}[/green]"
                    )
                    return True
        except (json.JSONDecodeError, KeyError):
            pass

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False) as tmp:
        tmp.write(public_key)
        tmp_path = tmp.name
    try:
        add = subprocess.run(
            ["gh", "ssh-key", "add", tmp_path, "--title", GITHUB_KEY_TITLE],
            capture_output=True, text=True,
        )
        if add.returncode == 0:
            console.print(f"Added SSH key to GitHub: [green]{GITHUB_KEY_TITLE}[/green]")
            return True
        console.print(f"[red]Failed to add key to GitHub: {add.stderr.strip()}[/red]")
        return False
    finally:
        os.unlink(tmp_path)


def ensure_github_key(existing_config):
    """Ensure a dedicated brr GitHub SSH key exists and is registered.

    Returns the private key path (suitable for ``GITHUB_SSH_KEY`` config),
    or an empty string if ``gh`` isn't available / authenticated.
    """
    ensure_state_dirs()

    key_path = _existing_brr_github_key()

    if not key_path:
        # Honor GITHUB_SSH_KEY only if it's already pointing at a dedicated
        # brr GitHub key. Provider cluster keys get replaced on next run.
        current = (existing_config or {}).get("GITHUB_SSH_KEY", "")
        if current and Path(current).name.startswith("github-") and Path(current).exists():
            key_path = current

    if not key_path:
        key_path = _generate_github_key()

    try:
        public_key = _read_public_key(key_path)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        return key_path  # Return path anyway so file_mounts still deliver something

    _register_with_github(public_key)
    return key_path


def remove_github_registration():
    """Delete brr-titled SSH keys from GitHub. Returns list of deleted IDs.

    Used by `brr nuke`. Removes all entries whose title starts with 'brr-'
    — covers both the new dedicated key and legacy per-provider entries
    (brr-aws, brr-nebius, brr-verda).
    """
    if not _gh_authenticated():
        return []
    deleted = []
    list_result = subprocess.run(
        ["gh", "api", "user/keys"], capture_output=True, text=True,
    )
    if list_result.returncode != 0:
        return deleted
    try:
        keys = json.loads(list_result.stdout)
    except (json.JSONDecodeError, KeyError):
        return deleted
    for k in keys:
        title = k.get("title", "")
        if not title.startswith("brr"):
            continue
        key_id = str(k.get("id", ""))
        if not key_id:
            continue
        result = subprocess.run(
            ["gh", "api", "-X", "DELETE", f"user/keys/{key_id}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            deleted.append(title)
            console.print(f"  Deleted GitHub SSH key: [red]{title}[/red]")
        else:
            console.print(f"  [yellow]Failed to delete {title}: {result.stderr.strip()}[/yellow]")
    return deleted
