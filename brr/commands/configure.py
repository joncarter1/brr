"""brr configure — interactive setup wizard."""

import os

import click
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.console import Console
from rich.panel import Panel

from brr.state import (
    ensure_state_dirs,
    read_config,
    write_config,
    CONFIG_PATH,
    CONFIG_DEFAULTS,
)

console = Console()

# AI coding tools: config key, display name
AI_TOOLS = [
    {"name": "Claude Code", "config_key": "INSTALL_CLAUDE_CODE"},
    {"name": "Codex", "config_key": "INSTALL_CODEX"},
    {"name": "Gemini CLI", "config_key": "INSTALL_GEMINI"},
]

GENERAL_DEFAULTS = {
    "CLUSTER_USER": os.environ.get("USER", "ubuntu"),
    "DOTFILES_REPO": "",
    **CONFIG_DEFAULTS,
}


def _run_provider_wizard(provider):
    """Check SDK and run provider-specific configure wizard."""
    try:
        if provider == "aws":
            import boto3  # noqa: F401
        else:
            import nebius  # noqa: F401
    except ImportError:
        raise click.ClickException(
            f"Missing dependencies for {provider}.\n"
            f"  Install with: uv tool install 'brr-cli[{provider}]'"
        )

    if provider == "aws":
        from brr.aws.configure import configure_aws

        configure_aws()
    else:
        from brr.nebius.configure import configure_nebius

        configure_nebius()


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.pass_context
def configure(ctx):
    """Interactive setup wizard for cluster instances.

    \b
    Run without arguments for the interactive menu, or use subcommands:
      brr configure cloud      Cloud provider credentials and infra
      brr configure tools      Pre-installed AI coding tools
      brr configure general    Instance settings (user, dotfiles, idle shutdown)
      brr configure aws        Shortcut for AWS cloud provider
      brr configure nebius     Shortcut for Nebius cloud provider
    """
    if ctx.invoked_subcommand is not None:
        return
    _interactive_menu(ctx)


def _interactive_menu(ctx):
    """Top-level interactive menu."""
    console.print(Panel("brr configure", border_style="cyan"))
    console.print()

    commands = {"cloud": cloud_cmd, "tools": tools_cmd, "general": general_cmd}
    while True:
        choice = inquirer.select(
            message="What would you like to configure?",
            choices=[
                Choice(
                    value="cloud",
                    name="Cloud provider — credentials and infrastructure",
                ),
                Choice(
                    value="tools",
                    name="AI coding tools — pre-installed on every instance",
                ),
                Choice(
                    value="general",
                    name="Instance settings — SSH user, dotfiles, idle shutdown",
                ),
                Choice(value="_exit", name="Exit"),
            ],
        ).execute()
        if choice == "_exit":
            break
        console.print()
        ctx.invoke(commands[choice])
        console.print()


@configure.command("cloud")
def cloud_cmd():
    """Configure cloud provider credentials and infrastructure."""
    console.print(
        Panel("Cloud provider", title="brr configure cloud", border_style="cyan")
    )
    console.print()

    provider = inquirer.select(
        message="Which cloud provider would you like to configure?",
        choices=[
            Choice(value="aws", name="AWS"),
            Choice(value="nebius", name="Nebius"),
        ],
    ).execute()
    console.print()

    _run_provider_wizard(provider)


@configure.command("tools")
def tools_cmd():
    """Choose AI coding tools to pre-install on cluster instances."""
    ensure_state_dirs()
    existing = read_config()

    console.print(
        Panel("AI coding tools", title="brr configure tools", border_style="cyan")
    )
    console.print(
        "[dim]Selected tools are installed on every cluster instance at boot.[/dim]"
    )
    console.print()

    tool_choices = [
        Choice(
            value=tool["config_key"],
            name=tool["name"],
            enabled=existing.get(tool["config_key"], "false").lower()
            in ("true", "yes", "1"),
        )
        for tool in AI_TOOLS
    ]
    selected = inquirer.checkbox(
        message="Select tools to install on every instance",
        choices=tool_choices,
        instruction="(space to toggle, enter to confirm)",
    ).execute()

    updates = {}
    for tool in AI_TOOLS:
        config_key = tool["config_key"]
        updates[config_key] = str(config_key in selected).lower()

    merged = dict(existing)
    merged.update(updates)
    write_config(merged)
    console.print(f"Wrote [green]{CONFIG_PATH}[/green]")


@configure.command("general")
def general_cmd():
    """Configure instance settings (SSH user, dotfiles, idle shutdown)."""
    ensure_state_dirs()
    existing = read_config()

    console.print(
        Panel("Instance settings", title="brr configure general", border_style="cyan")
    )
    console.print()

    cluster_user = click.prompt(
        "Cluster user",
        default=existing.get("CLUSTER_USER", GENERAL_DEFAULTS["CLUSTER_USER"]),
    )
    dotfiles_repo = click.prompt(
        "Dotfiles repo (blank to skip)",
        default=existing.get("DOTFILES_REPO", ""),
        show_default=False,
    )

    idle_default = existing.get(
        "IDLE_SHUTDOWN_ENABLED", GENERAL_DEFAULTS["IDLE_SHUTDOWN_ENABLED"]
    )
    idle_enabled = click.confirm(
        "Enable idle shutdown?",
        default=idle_default.lower() in ("true", "yes", "1"),
    )
    idle_timeout = idle_threshold = idle_grace = None
    if idle_enabled:
        idle_timeout = click.prompt(
            "  Idle timeout (minutes)",
            default=int(
                existing.get(
                    "IDLE_SHUTDOWN_TIMEOUT_MIN",
                    GENERAL_DEFAULTS["IDLE_SHUTDOWN_TIMEOUT_MIN"],
                )
            ),
            type=int,
        )
        idle_threshold = click.prompt(
            "  CPU threshold (%)",
            default=int(
                existing.get(
                    "IDLE_SHUTDOWN_CPU_THRESHOLD",
                    GENERAL_DEFAULTS["IDLE_SHUTDOWN_CPU_THRESHOLD"],
                )
            ),
            type=int,
        )
        idle_grace = click.prompt(
            "  Grace period (minutes)",
            default=int(
                existing.get(
                    "IDLE_SHUTDOWN_GRACE_MIN",
                    GENERAL_DEFAULTS["IDLE_SHUTDOWN_GRACE_MIN"],
                )
            ),
            type=int,
        )

    merged = dict(existing)
    merged.update(
        {
            "CLUSTER_USER": cluster_user,
            "DOTFILES_REPO": dotfiles_repo,
            "IDLE_SHUTDOWN_ENABLED": str(idle_enabled).lower(),
            "IDLE_SHUTDOWN_TIMEOUT_MIN": str(
                idle_timeout or GENERAL_DEFAULTS["IDLE_SHUTDOWN_TIMEOUT_MIN"]
            ),
            "IDLE_SHUTDOWN_CPU_THRESHOLD": str(
                idle_threshold or GENERAL_DEFAULTS["IDLE_SHUTDOWN_CPU_THRESHOLD"]
            ),
            "IDLE_SHUTDOWN_GRACE_MIN": str(
                idle_grace or GENERAL_DEFAULTS["IDLE_SHUTDOWN_GRACE_MIN"]
            ),
        }
    )
    write_config(merged)
    console.print(f"\nWrote [green]{CONFIG_PATH}[/green]")


@configure.command("aws")
def aws_cmd():
    """Shortcut: configure AWS cloud provider."""
    _run_provider_wizard("aws")


@configure.command("nebius")
def nebius_cmd():
    """Shortcut: configure Nebius cloud provider."""
    _run_provider_wizard("nebius")
