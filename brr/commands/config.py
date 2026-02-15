import click
from rich.console import Console

from brr.state import (
    read_config, write_config, CONFIG_PATH,
    find_project_root, read_project_config,
)

console = Console()

# Section grouping by key prefix. Order matters — first match wins.
# None means catch-all.
_SECTIONS = [
    ("AWS", ["AWS_", "EFS_", "AMI_", "EC2_SSH"]),
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
    global_cfg = read_config()
    if not global_cfg:
        raise click.ClickException("No config found. Run 'brr configure' first.")

    project_root = find_project_root()
    project_cfg = read_project_config(project_root) if project_root else {}

    # Merge: global + project overlay
    merged = dict(global_cfg)
    merged.update(project_cfg)

    # Group keys by section
    sections = {}
    for key in merged:
        section = _section_for(key)
        sections.setdefault(section, []).append(key)

    # Find max key length for alignment
    max_key_len = max(len(k) for k in merged) if merged else 0

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
            value = merged[key]
            line = f"  {key:<{max_key_len}}  {value}"
            if key in project_cfg:
                console.print(f"{line}  [dim](project)[/dim]")
            else:
                console.print(line)

    # Footer: source files
    console.print()
    if project_cfg:
        n = len(project_cfg)
        console.print(
            f"[dim]{CONFIG_PATH} + .brr/config.env ({n} override{'s' if n != 1 else ''})[/dim]"
        )
    else:
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
