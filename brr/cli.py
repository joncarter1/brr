import os

# Silence gRPC/abseil log spam from the Nebius SDK
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

import click
from importlib.metadata import version as _pkg_version

from brr.commands.bake import bake
from brr.commands.config import config
from brr.commands.init import init_cmd
from brr.commands.nuke import nuke
from brr.commands.configure import configure
from brr.cluster import (
    up, down, attach, clean, vscode, list_cmd, templates,
)
from brr.update import print_update_notice


@click.group()
@click.version_option(version=_pkg_version("brr-cli"), prog_name="brr")
def cli():
    """Cluster management CLI.

    \b
    Quick start:
      brr configure    Set up cloud provider credentials
      brr init         Initialize project templates
      brr up aws:dev   Launch a cluster
    """
    print_update_notice()


@cli.command("completion")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]), default="bash")
@click.option("--install", is_flag=True, help="Append completion to your shell rc file")
def completion(shell, install):
    """Print shell completion script.

    \b
    Usage:
      eval "$(brr completion bash)"         # current session
      brr completion bash --install         # persist to rc file
    """
    import os
    import subprocess
    from pathlib import Path

    env_var = "_BRR_COMPLETE"
    env_val = f"{shell}_source"
    result = subprocess.run(
        ["brr"],
        env={**os.environ, env_var: env_val},
        capture_output=True,
        text=True,
    )
    output = result.stdout
    # macOS ships bash 3.2 which doesn't support 'nosort'
    if shell == "bash":
        output = output.replace(" -o nosort", "")

    if not install:
        click.echo(output)
        return

    rc_files = {"bash": "~/.bashrc", "zsh": "~/.zshrc", "fish": "~/.config/fish/config.fish"}
    rc_path = Path(os.path.expanduser(rc_files[shell]))

    eval_line = f'eval "$(brr completion {shell})"'
    if shell == "fish":
        eval_line = f"brr completion fish | source"

    # Check if already installed
    if rc_path.exists() and eval_line in rc_path.read_text():
        click.echo(f"Completion already installed in {rc_path}")
        return

    rc_path.parent.mkdir(parents=True, exist_ok=True)
    with open(rc_path, "a") as f:
        f.write(f"\n# brr shell completion\n{eval_line}\n")

    click.echo(f"Completion installed in {rc_path}")
    click.echo(f"Run 'source {rc_path}' or open a new terminal to activate.")


# Cluster lifecycle
cli.add_command(up)
cli.add_command(down)
cli.add_command(attach)
cli.add_command(clean)
cli.add_command(vscode)
cli.add_command(list_cmd, "list")

# Template inspection
cli.add_command(templates)

# Image baking
cli.add_command(bake)

# Project setup
cli.add_command(init_cmd, "init")

# Configuration
cli.add_command(config)
cli.add_command(configure)

# Destructive
cli.add_command(nuke)
