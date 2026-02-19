import click
from rich.console import Console

from brr.state import read_config, write_config, CONFIG_PATH

console = Console()

# Section grouping by key prefix. Order matters — first match wins.
# None means catch-all.
_SECTIONS = [
    ("AWS", ["AWS_", "EFS_", "AMI_"]),
    ("Nebius", ["NEBIUS_"]),
    ("Idle Shutdown", ["IDLE_SHUTDOWN_"]),
    ("General", None),
]


def _section_for(key):
    """Return the section name for a config key."""
    for name, prefixes in _SECTIONS:
        if prefixes is None:
            return name
        if any(key.startswith(p) for p in prefixes):
            return name
    return "General"


@click.group(invoke_without_command=True)
@click.pass_context
def config(ctx):
    """Show or modify configuration."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_cmd)


@config.command("list")
def list_cmd():
    """List all configuration values."""
    cfg = read_config()
    if not cfg:
        raise click.ClickException("No config found. Run 'brr configure' first.")

    # Group keys by section
    sections = {}
    for key in cfg:
        section = _section_for(key)
        sections.setdefault(section, []).append(key)

    # Find max key length for alignment
    max_key_len = max(len(k) for k in cfg) if cfg else 0

    # Render sections in defined order
    first = True
    for section_name, _ in _SECTIONS:
        keys = sections.get(section_name)
        if not keys:
            continue
        if not first:
            console.print()
        first = False
        console.print(f"[bold]{section_name}[/bold]")
        for key in keys:
            value = cfg[key]
            console.print(f"  {key:<{max_key_len}}  {value}")

    # Footer: source file
    console.print()
    console.print(f"[dim]{CONFIG_PATH}[/dim]")


@config.command("get")
@click.argument("key")
def get_cmd(key):
    """Get a single configuration value."""
    cfg = read_config()
    if not cfg:
        raise click.ClickException("No config found. Run 'brr configure' first.")
    key = key.upper()
    if key not in cfg:
        raise click.ClickException(f"Unknown key: {key}")
    click.echo(cfg[key])


@config.command("set")
@click.argument("key")
@click.argument("value")
def set_cmd(key, value):
    """Set a configuration value."""
    cfg = read_config()
    if not cfg:
        raise click.ClickException("No config found. Run 'brr configure' first.")
    key = key.upper()
    cfg[key] = value
    write_config(cfg)
    click.echo(f"{key}={value}")


@config.command("path")
def path_cmd():
    """Print config file path."""
    click.echo(CONFIG_PATH)
