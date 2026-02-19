"""Shared SSH utilities for brr — provider-agnostic."""

import json
import os
import re
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()


def update_ssh_config(host_alias, head_ip, ssh_key):
    """Write or update a Host block in ~/.ssh/config.

    host_alias is the full SSH alias (e.g. 'brr-aws-h100', 'brr-nebius-h100').
    """

    block = (
        f"Host {host_alias}\n"
        f"    HostName {head_ip}\n"
        f"    User ubuntu\n"
        f"    IdentityFile {ssh_key}\n"
        f"    StrictHostKeyChecking accept-new\n"
        f"    ProxyCommand none\n"
    )

    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    config_path = ssh_dir / "config"

    if config_path.exists():
        content = config_path.read_text()
    else:
        content = ""

    # Replace existing block or append
    pattern = re.compile(
        rf"^Host {re.escape(host_alias)}\n(?:[ \t]+\S.*\n)*",
        re.MULTILINE,
    )
    if pattern.search(content):
        content = pattern.sub(block, content)
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n" + block

    config_path.write_text(content)
    os.chmod(config_path, 0o600)

    console.print(f"[dim]Updated SSH config: {host_alias} -> {head_ip}[/dim]")
    console.print(
        f'[dim]VS Code: Cmd+Shift+P -> "Remote-SSH: Connect to Host" -> {host_alias}[/dim]'
    )


def remove_ssh_config(host_alias):
    """Remove a Host block from ~/.ssh/config."""
    config_path = Path.home() / ".ssh" / "config"
    if not config_path.exists():
        return
    content = config_path.read_text()
    pattern = re.compile(
        rf"^Host {re.escape(host_alias)}\n(?:[ \t]+\S.*\n)*",
        re.MULTILINE,
    )
    new_content = pattern.sub("", content)
    if new_content != content:
        config_path.write_text(new_content)
        console.print(f"[dim]Removed SSH config: {host_alias}[/dim]")


def get_ray_status(head_ip, ssh_key):
    """SSH into a head node and query Ray for node/resource status.

    Returns dict with 'nodes', 'cpu', 'gpu' or None on failure.
    """
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
        "-i", ssh_key, f"ubuntu@{head_ip}",
        'source /tmp/brr/venv/bin/activate && python -c "'
        "import ray, json; "
        "ray.init(address='auto', ignore_reinit_error=True); "
        "nodes = [n for n in ray.nodes() if n['Alive']]; "
        "res = ray.cluster_resources(); "
        "print(json.dumps({'nodes': len(nodes), "
        "'cpu': int(res.get('CPU', 0)), "
        "'gpu': int(res.get('GPU', 0))}))"
        '"',
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return json.loads(result.stdout.strip())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass
    return None
