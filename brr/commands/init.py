import subprocess

import click
from pathlib import Path

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from brr.state import is_provider_configured, CONFIG_DEFAULTS
from brr.templates import _template_dir


PROVIDERS = ["aws", "nebius"]

# Maps project template name → built-in template to copy from, per provider.
_TEMPLATE_MAP = {
    "aws": {
        "dev": "l4",         # single L4 GPU
        "cluster": "cpu-l4", # CPU head + L4 GPU workers
    },
    "nebius": {
        "dev": "h100",         # single H100 GPU
        "cluster": "cpu-h100s", # CPU head + H100 workers
    },
}


def _find_repo_root():
    """Find the git repo root from CWD, or fall back to CWD."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return Path.cwd().resolve()


def _read_builtin(provider, builtin_name):
    """Read a built-in template's content."""
    tpl_dir = _template_dir(provider)
    return tpl_dir.joinpath(f"{builtin_name}.yaml").read_text()


@click.command("init")
def init_cmd():
    """Initialize a project for brr.

    Shows an interactive provider selection, then creates a .brr/ directory
    in the current repo with two templates per provider:

    \b
      dev      Single GPU machine for development
      cluster  CPU head + GPU workers

    Both share the same EFS/Nebius filesystem, so ~/code/ is synced.

    Then run `brr up dev` to launch a cluster and sync your code.
    """
    # Check that at least one provider is configured
    configured = [p for p in PROVIDERS if is_provider_configured(p)]
    if not configured:
        raise click.ClickException(
            "No cloud provider configured. Run `brr configure` first."
        )

    # Provider selection — configured providers pre-checked, unconfigured greyed out
    provider_choices = []
    for p in PROVIDERS:
        if p in configured:
            provider_choices.append(Choice(value=p, name=p.upper(), enabled=True))
        else:
            provider_choices.append({"value": p, "name": p.upper(), "disabled": "not configured"})
    providers = inquirer.checkbox(
        message="Select providers to initialize",
        choices=provider_choices,
        instruction="(space to toggle, enter to confirm)",
    ).execute()
    if not providers:
        click.echo("No providers selected.")
        return

    project_root = _find_repo_root()
    brr_dir = project_root / ".brr"
    repo_name = project_root.name

    # Check for existing templates per provider
    for provider in list(providers):
        pdir = brr_dir / provider
        if pdir.is_dir() and any(pdir.glob("*.yaml")):
            existing = [p.stem for p in pdir.glob("*.yaml")]
            click.echo(f"Already initialized for {provider} — found templates: {', '.join(existing)}")
            if not click.confirm("Overwrite?"):
                providers = [p for p in providers if p != provider]

    if not providers:
        return

    # Detect uv-managed project
    pyproject = project_root / "pyproject.toml"
    lockfile = project_root / "uv.lock"
    is_uv_project = pyproject.exists() and lockfile.exists()

    for provider in providers:
        tpl_map = _TEMPLATE_MAP[provider]
        provider_dir = brr_dir / provider
        provider_dir.mkdir(parents=True, exist_ok=True)

        for project_name, builtin_name in tpl_map.items():
            content = _read_builtin(provider, builtin_name)
            content = content.replace(
                f"cluster_name: {builtin_name}",
                f"cluster_name: {repo_name}-{project_name}",
                1,
            )
            dest = provider_dir / f"{project_name}.yaml"
            dest.write_text(content)

        if is_uv_project:
            extra = f"brr-cli[{provider}]"
            click.echo(f"\nDetected uv project — adding {extra} and ray[default] to brr dependency group...")
            subprocess.run(
                ["uv", "add", "--group", "brr", extra, "ray[default]"],
                cwd=str(project_root), check=False,
            )

        # Write a small project setup stub (global setup handles the heavy lifting)
        if is_uv_project:
            setup_stub = f"""\
#!/bin/bash
# Project setup — runs after global setup on every node boot.
set -Eeuo pipefail

# Sync project dependencies (uses locked versions from uv.lock)
if [ -d "$HOME/code/{repo_name}" ]; then
  cd "$HOME/code/{repo_name}"
  # Pre-fetch the Python version required by the project so uv sync doesn't hang.
  uv python install
  uv sync --group brr
fi

# Add extra project-specific dependencies below:
# uv pip install torch
"""
        else:
            setup_stub = """\
#!/bin/bash
# Project setup — runs after global setup on every node boot.
# Global setup (~/.brr/setup.sh) already provides:
#   packages, mounts, venv, Ray, Claude Code, SSH, dotfiles.
# Add project-specific dependencies below.
set -Eeuo pipefail

source "$HOME/.venv/bin/activate"
# uv pip install torch
# uv pip install jax[cuda12]
"""
        (provider_dir / "setup.sh").write_text(setup_stub)

        click.echo(f"\nInitialized {provider} project in .brr/{provider}/")
        click.echo(f"  .brr/{provider}/dev.yaml      Single GPU ({repo_name}-dev)")
        click.echo(f"  .brr/{provider}/cluster.yaml  CPU head + GPU workers ({repo_name}-cluster)")
        click.echo(f"  .brr/{provider}/setup.sh      Project deps (runs after global setup)")

    # Scaffold project config.env if it doesn't exist
    project_config = brr_dir / "config.env"
    if not project_config.exists():
        project_config.write_text(
            '# Project config — overrides ~/.brr/config.env for this project.\n'
            '# Uncomment and edit values as needed.\n'
            '\n'
            f'# IDLE_SHUTDOWN_TIMEOUT_MIN="{CONFIG_DEFAULTS["IDLE_SHUTDOWN_TIMEOUT_MIN"]}"\n'
            '# DOTFILES_REPO="https://github.com/user/dotfiles"\n'
            '# PYTHON_VERSION="3.11"\n'
        )
        click.echo(f"  .brr/config.env               Project config (overrides global)")

    click.echo(f"\nTemplates are standard Ray YAML — edit them or add your own.")
    click.echo(f"\nLaunch:")
    click.echo(f"  brr up dev            # start dev machine")
    click.echo(f"  brr up cluster        # start full cluster")
    click.echo(f"  brr up dev --dry-run  # preview config")
