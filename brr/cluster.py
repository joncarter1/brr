import shlex
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.table import Table

from brr.state import (
    read_config, read_merged_config, find_project_root, find_project_providers,
    resolve_project_provider, read_project_config, parse_provider, cluster_ssh_alias,
    check_provider_configured,
)
from brr.templates import (
    resolve_template,
    resolve_default_template,
    render,
    apply_overrides,
    apply_baked_images,
    global_setup_hash,
    prepare_staging,
    inject_brr_infra,
    rewrite_ray_commands_for_uv,
    write_yaml,
    output_path_for,
    list_templates,
    find_project_templates,
    extract_template_aliases,
    check_required,
    find_required,
    _get_nested,
    GLOBAL_ARGS,
)

console = Console()


def _resolve_provider(name):
    """Parse provider:name syntax, inferring provider from project if no prefix.

    Returns (provider, template_or_cluster_name, explicit).
    - Explicit prefix (aws:dev) → explicit=True, provider from prefix
    - Bare name (dev) → explicit=False, provider inferred from project
    - File path (./foo.yaml) → explicit=False, default provider
    """
    explicit = ":" in name and not name.endswith(".yaml") and "/" not in name
    provider, parsed_name = parse_provider(name)
    if not explicit:
        project_root = find_project_root()
        if project_root is not None:
            project_config = read_project_config(project_root)
            inferred = resolve_project_provider(project_root, project_config)
            if inferred:
                provider = inferred
    return provider, parsed_name, explicit


def _project_root_for(provider, tpl_name, explicit):
    """Find project_root relevant to this template.

    Explicit provider:name → project if that template exists there, else None (built-in).
    Bare name → project if found, else None.
    """
    project_root = find_project_root()
    if explicit and project_root is not None:
        project_tpl = Path(project_root) / ".brr" / provider / f"{tpl_name}.yaml"
        if not project_tpl.exists():
            return None  # not in project, fall to built-in
    return project_root


def _find_ray():
    """Find the ray CLI binary. Checks the current venv first, then PATH."""
    from pathlib import Path

    venv_ray = Path(sys.executable).parent / "ray"
    if venv_ray.is_file():
        return str(venv_ray)

    on_path = shutil.which("ray")
    if on_path:
        return on_path

    console.print("[red]Ray is not installed.[/red]")
    console.print("Install it with: [bold]pip install brr[/bold]")
    raise SystemExit(1)


def _run_ray(args):
    """Run a ray CLI command, passing through stdout/stderr."""
    ray_bin = _find_ray()
    cmd = [ray_bin] + args
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def _extract_cluster_name(path):
    """Extract cluster_name from a YAML template without full parsing.

    Templates may contain {{VAR}} placeholders that aren't valid YAML,
    but cluster_name is always a plain string on its own line.
    """
    import re
    for line in Path(path).read_text().splitlines():
        m = re.match(r'^cluster_name:\s*(\S+)', line)
        if m:
            return m.group(1)
    return None


def _resolve_cluster_name(tpl_name, provider, project_root=None):
    """Resolve template name to cluster_name from project YAML.

    Falls back to tpl_name if not in a project or no cluster_name field.
    """
    if project_root and "/" not in tpl_name and not tpl_name.endswith(".yaml"):
        tpl_path = Path(project_root) / ".brr" / provider / f"{tpl_name}.yaml"
        if tpl_path.exists():
            name = _extract_cluster_name(tpl_path)
            if name:
                return name
    return tpl_name


def _project_cluster_map(project_root):
    """Return mapping of cluster_name → template_stem for project templates."""
    mapping = {}
    brr_dir = Path(project_root) / ".brr"
    for yaml_file in brr_dir.glob("*/*.yaml"):
        name = _extract_cluster_name(yaml_file)
        if name:
            mapping[name] = yaml_file.stem
    return mapping


def _staging_project_map():
    """Scan rendered YAMLs in staging to map cluster_name → project_path.

    Reads file_mounts from each rendered YAML — if /home/ubuntu/_project/
    is mounted, the source path is the local project root.
    """
    import re
    from brr.state import STATE_DIR

    mapping = {}  # cluster_name → project_path_str
    staging = STATE_DIR / "staging"
    if not staging.is_dir():
        return mapping

    for yaml_file in staging.glob("**/*.yaml"):
        cluster_name = None
        project_path = None
        for line in yaml_file.read_text().splitlines():
            if cluster_name is None:
                m = re.match(r'^cluster_name:\s*(\S+)', line)
                if m:
                    cluster_name = m.group(1)
            if project_path is None:
                m = re.match(r'\s+/home/ubuntu/_project/:\s*(.+?)/?$', line)
                if m:
                    project_path = m.group(1)
            if cluster_name and project_path:
                break
        if cluster_name and project_path:
            mapping[cluster_name] = project_path
    return mapping


def _sync_ssh_config_aws(cluster_name, config, short_name=None):
    """Query EC2 for the cluster head IP and update local SSH config."""
    from brr.aws.nodes import query_ray_clusters, update_ssh_config

    region = config.get("AWS_REGION", "us-east-1")
    clusters = query_ray_clusters(region)
    match = next(
        (c for c in clusters if c["cluster_name"] == cluster_name and c["state"] == "running"),
        None,
    )
    if match and match["head_ip"] != "-":
        ssh_alias = cluster_ssh_alias("aws", cluster_name)
        update_ssh_config(ssh_alias, match["head_ip"], config.get("AWS_SSH_KEY", ""))
        attach_name = short_name or cluster_name
        console.print(f"Updated local SSH config: [green]{ssh_alias}[/green]")
        console.print(f"  Connect with: [bold]brr attach {attach_name}[/bold]")
        console.print(f"  VS Code:      [bold]brr vscode {attach_name}[/bold]")


def _sync_ssh_config_nebius(cluster_name, config, short_name=None):
    """Query Nebius for the cluster head IP and update local SSH config."""
    from brr.aws.nodes import update_ssh_config
    from brr.nebius.nodes import query_head_ip

    project_id = config.get("NEBIUS_PROJECT_ID", "")
    if not project_id:
        return
    head_ip = query_head_ip(project_id, cluster_name)
    if head_ip:
        ssh_alias = cluster_ssh_alias("nebius", cluster_name)
        update_ssh_config(ssh_alias, head_ip, config.get("NEBIUS_SSH_KEY", ""))
        attach_name = short_name or f"nebius:{cluster_name}"
        console.print(f"Updated local SSH config: [green]{ssh_alias}[/green]")
        console.print(f"  Connect with: [bold]brr attach {attach_name}[/bold]")
        console.print(f"  VS Code:      [bold]brr vscode {attach_name}[/bold]")
    else:
        console.print(f"[yellow]Warning: could not find head IP for Nebius cluster '{cluster_name}'[/yellow]")


def _sync_ssh_config(provider, cluster_name, short_name=None):
    """Query the cloud for the cluster head IP and update local SSH config."""
    config = read_config()
    if not config:
        return
    if provider == "nebius":
        _sync_ssh_config_nebius(cluster_name, config, short_name=short_name)
    else:
        _sync_ssh_config_aws(cluster_name, config, short_name=short_name)


@click.command()
@click.argument("template", required=False, default=None)
@click.argument("overrides", nargs=-1)
@click.option("--no-config-cache", is_flag=True, help="Disable Ray config cache")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
@click.option("--dry-run", is_flag=True, help="Print rendered config without launching")
def up(template, overrides, no_config_cache, yes, dry_run):
    """Launch or update a Ray cluster.

    TEMPLATE is a built-in with provider prefix (aws:h100, nebius:cpu),
    a project template name (dev, cluster), or a .yaml file path.

    If omitted inside a project with .brr/, the default project template
    is used automatically and the repo is synced to ~/code/ on the cluster.

    OVERRIDES are key=value pairs applied to the rendered YAML.
    Use `brr templates show TEMPLATE` to see available overrides.

    \b
      instance_type=p5.48xlarge    Override head node instance type
      max_workers=4                Set max workers
      spot=true                    Enable spot pricing
      region=us-west-2             Override region
      az=us-east-1a                Override availability zone
      provider.region=us-west-2    Dot-notation for arbitrary YAML paths
    """
    import yaml

    # Project auto-discovery
    project_root = find_project_root()

    if template is None:
        if project_root is None:
            raise click.UsageError(
                "No TEMPLATE given and no .brr/ project found.\n"
                "Run `brr init` to set up a project, or specify a template: brr up <template>"
            )
        project_config = read_project_config(project_root)
        provider = resolve_project_provider(project_root, project_config)
        tpl_name = resolve_default_template(project_root, project_config, provider)
        console.print(f"Project: [bold]{project_root.name}[/bold]")
    else:
        provider, tpl_name, explicit = _resolve_provider(template)
        project_root = _project_root_for(provider, tpl_name, explicit)
        if not explicit and project_root is None:
            # Bare name outside a project — require prefix
            raise click.UsageError(
                f"Template '{tpl_name}' requires a project (.brr/ directory).\n"
                f"Use a provider prefix for built-in templates: brr up {provider}:{tpl_name}\n"
                f"Or run `brr init` to set up a project in this directory."
            )

    config, _ = read_merged_config(project_root)
    check_provider_configured(provider, config)

    try:
        tpl_content, tpl_name = resolve_template(tpl_name, provider, project_root=project_root)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)

    console.print(f"Template: [bold cyan]{tpl_name}[/bold cyan]")

    rendered = render(tpl_content, config)
    template_aliases = extract_template_aliases(rendered)

    if overrides:
        console.print(f"Overrides: [yellow]{' '.join(overrides)}[/yellow]")
        try:
            rendered = apply_overrides(rendered, overrides, template_aliases)
        except (ValueError, KeyError) as e:
            console.print(f"[red]Override error: {e}[/red]")
            raise SystemExit(1)

    check_required(rendered, template_aliases)
    rewrite_ray_commands_for_uv(rendered, project_root)

    if dry_run:
        console.print()
        console.print(yaml.dump(rendered, default_flow_style=False, sort_keys=False), end="", highlight=False)
        return

    cluster_name = rendered.get("cluster_name", tpl_name)

    # If re-deploying a project, ask whether to sync local files to the cluster.
    sync_project = True
    if project_root and not dry_run:
        out_path = output_path_for(cluster_name, provider)
        if out_path.exists():
            sync_project = yes or click.confirm(
                "Project may already exist on cluster. Sync local files?",
                default=True,
            )

    staging = prepare_staging(cluster_name, provider, project_root=project_root)
    inject_brr_infra(rendered, staging, repo_root=project_root if sync_project else None)

    # Apply baked images if available (works for both AWS and Nebius)
    apply_baked_images(rendered, config)

    if provider == "aws":
        bake_hash = config.get("BAKE_SETUP_HASH", "")
        has_baked = config.get("AMI_UBUNTU_BAKED") or config.get("AMI_DL_BAKED")
        if has_baked and bake_hash and bake_hash != global_setup_hash():
            console.print(
                "[yellow]Warning: ~/.brr/setup.sh has changed since last bake. "
                "Run `brr bake aws` to rebuild.[/yellow]"
            )
        elif not has_baked:
            console.print("[dim]Tip: Run `brr bake aws` to pre-bake setup into AMIs for faster boot.[/dim]")
    elif provider == "nebius":
        bake_hash = config.get("NEBIUS_BAKE_SETUP_HASH", "")
        has_baked = config.get("NEBIUS_IMAGE_CPU_BAKED") or config.get("NEBIUS_IMAGE_GPU_BAKED")
        if has_baked and bake_hash and bake_hash != global_setup_hash():
            console.print(
                "[yellow]Warning: ~/.brr/setup.sh has changed since last bake. "
                "Run `brr bake nebius` to rebuild.[/yellow]"
            )
        elif not has_baked:
            console.print("[dim]Tip: Run `brr bake nebius` to pre-bake setup into images for faster boot.[/dim]")

    if project_root and sync_project:
        console.print(f"Repo sync: [green]{project_root.name}[/green] → ~/code/{project_root.name}/")
    elif project_root:
        console.print(f"Repo sync: [dim]skipped[/dim]")

    out = output_path_for(cluster_name, provider)
    write_yaml(rendered, out)
    console.print(f"Wrote [green]{out}[/green]")

    ray_args = ["up", str(out)]
    if no_config_cache:
        ray_args.append("--no-config-cache")
    if yes:
        ray_args.append("-y")

    ray_bin = _find_ray()
    cmd = [ray_bin] + ray_args
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    ray_result = subprocess.run(cmd)

    # Post-ray: sync SSH config even if ray up had warnings (non-zero exit)
    try:
        if provider == "aws":
            from brr.aws.nodes import ensure_secretsmanager_iam, query_ray_clusters, update_ssh_config

            if config.get("GITHUB_SSH_SECRET"):
                ensure_secretsmanager_iam(config.get("AWS_REGION", "us-east-1"))

            region = config.get("AWS_REGION", "us-east-1")
            clusters = query_ray_clusters(region)
            match = next(
                (c for c in clusters if c["cluster_name"] == cluster_name and c["state"] == "running"),
                None,
            )
            if match and match["head_ip"] != "-":
                ssh_alias = cluster_ssh_alias("aws", cluster_name)
                update_ssh_config(
                    ssh_alias,
                    match["head_ip"],
                    config.get("AWS_SSH_KEY", ""),
                )
                console.print(f"Updated local SSH config: [green]{ssh_alias}[/green]")
                attach_name = tpl_name if project_root else cluster_name
                console.print(f"  Connect with: [bold]brr attach {attach_name}[/bold]")
                console.print(f"  VS Code:      [bold]brr vscode {attach_name}[/bold]")
        else:
            _sync_ssh_config(provider, cluster_name, short_name=tpl_name if project_root else None)
    except Exception as e:
        console.print(f"[yellow]Warning: SSH config sync failed: {e}[/yellow]")

    if ray_result.returncode != 0:
        sys.exit(ray_result.returncode)


@click.command()
@click.argument("template")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
@click.option("--delete", is_flag=True, help="Full cleanup: terminate all instances, remove local staging files")
def down(template, yes, delete):
    """Stop a cluster (preserving instances for fast restart).

    Instances are stopped, not terminated — local disk data is preserved and
    the cluster restarts quickly on the next `brr up`. No hourly charges while
    stopped (EBS storage charges still apply).

    With --delete, terminates all instances (data lost) and removes local
    staging files. Use `brr clean` to selectively terminate stopped instances.
    """
    import shutil
    from pathlib import Path
    from brr.aws.nodes import remove_ssh_config
    from brr.state import staging_dir_for, rendered_yaml_for

    provider, tpl_name, explicit = _resolve_provider(template)
    project_root = _project_root_for(provider, tpl_name, explicit)
    cluster_name = _resolve_cluster_name(tpl_name, provider, project_root)
    ssh_alias = cluster_ssh_alias(provider, cluster_name)

    if "/" in tpl_name or tpl_name.endswith(".yaml"):
        yaml_path = tpl_name
    else:
        yaml_path = str(output_path_for(cluster_name, provider))

    # Run ray down if YAML exists
    yaml_file = Path(yaml_path)
    if yaml_file.exists():
        ray_args = ["down", yaml_path]
        if yes or delete:
            ray_args.append("-y")
        _run_ray(ray_args)
    elif delete:
        console.print(f"[yellow]No rendered YAML at {yaml_path}, skipping ray down.[/yellow]")
    else:
        console.print(f"[red]No rendered YAML at {yaml_path}. Did you mean --delete?[/red]")
        raise SystemExit(1)

    # Clean up SSH config entry
    remove_ssh_config(ssh_alias)

    if not delete:
        return

    # Terminate any remaining cloud instances
    config = read_config()

    if provider == "nebius":
        project_id = config.get("NEBIUS_PROJECT_ID", "")
        if project_id:
            from brr.nebius.nodes import terminate_cluster_instances
            with console.status(f"[bold green]Terminating Nebius instances for '{cluster_name}'..."):
                count = terminate_cluster_instances(project_id, cluster_name)
            if count:
                console.print(f"[green]Terminated {count} Nebius instance(s).[/green]")
    else:
        region = config.get("AWS_REGION", "us-east-1")
        from brr.aws.nodes import terminate_cluster_instances
        with console.status(f"[bold green]Terminating AWS instances for '{cluster_name}'..."):
            count = terminate_cluster_instances(region, cluster_name)
        if count:
            console.print(f"[green]Terminated {count} AWS instance(s).[/green]")

    # Remove local staging files
    staging = staging_dir_for(cluster_name, provider)
    rendered = rendered_yaml_for(cluster_name, provider)

    if staging.exists():
        shutil.rmtree(staging)
        console.print(f"Removed staging: [dim]{staging}[/dim]")

    if rendered.exists():
        rendered.unlink()
        console.print(f"Removed config: [dim]{rendered}[/dim]")


@click.command()
@click.argument("cluster")
@click.argument("command", nargs=-1)
def attach(cluster, command):
    """SSH into the head node of a Ray cluster.

    CLUSTER is a template name (aws:h100, dev) or cluster name.
    Use provider:name syntax for built-in templates (e.g. nebius:h100).
    Uses the SSH config entry written by `brr up`.

    Optionally pass a COMMAND to run on the node (e.g. brr attach aws:h100 claude).
    """
    provider, name, explicit = _resolve_provider(cluster)
    project_root = _project_root_for(provider, name, explicit)
    cluster_name = _resolve_cluster_name(name, provider, project_root)
    host = cluster_ssh_alias(provider, cluster_name)

    if command:
        remote_cmd = " ".join(shlex.quote(c) for c in command)
        console.print(f"[dim]$ ssh -t {host} bash -lc {remote_cmd}[/dim]")
        result = subprocess.run(["ssh", "-t", host, f"bash -lc {shlex.quote(remote_cmd)}"])
    else:
        console.print(f"[dim]$ ssh {host}[/dim]")
        result = subprocess.run(["ssh", host])
    sys.exit(result.returncode)


@click.command()
@click.argument("template", required=False)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def clean(template, yes):
    """Terminate stopped (cached) instances.

    Stopped instances are left behind by `brr down` (which stops rather than
    terminates). Use this to free them — e.g. after `brr bake` when cached
    instances have stale AMIs.

    If TEMPLATE is given, clean only that cluster. Otherwise clean all stopped
    Ray instances.
    """
    config = read_config()

    if template:
        provider, tpl_name, explicit = _resolve_provider(template)
        project_root = _project_root_for(provider, tpl_name, explicit)
        cluster_name = _resolve_cluster_name(tpl_name, provider, project_root)
    else:
        provider, cluster_name = "aws", None

    check_provider_configured(provider, config)

    if provider == "nebius":
        _clean_nebius(config, cluster_name, yes)
    else:
        _clean_aws(config, cluster_name, yes)


def _clean_nebius(config, cluster_name, yes):
    from brr.nebius.nodes import query_stopped_instances, terminate_instances

    project_id = config.get("NEBIUS_PROJECT_ID", "")
    if not project_id:
        console.print("[red]NEBIUS_PROJECT_ID not configured. Run `brr configure nebius` first.[/red]")
        return

    with console.status("[bold green]Querying stopped Nebius instances..."):
        stopped = query_stopped_instances(project_id, cluster_name)

    if not stopped:
        label = f"cluster '{cluster_name}'" if cluster_name else "any cluster"
        console.print(f"[dim]No stopped Nebius instances for {label}.[/dim]")
        return

    by_cluster = defaultdict(list)
    for inst in stopped:
        by_cluster[inst["cluster_name"]].append(inst)

    total = len(stopped)
    for name, instances in sorted(by_cluster.items()):
        console.print(f"  [cyan]{name}[/cyan]: {len(instances)} stopped instance(s)")
    console.print(f"\nTotal: [bold]{total}[/bold] instance(s).")

    if not yes and not click.confirm("Terminate?", default=True):
        return

    ids = [inst["instance_id"] for inst in stopped]
    with console.status("[bold green]Terminating instances..."):
        count = terminate_instances(project_id, ids)
    console.print(f"[green]Terminated {count} instance(s).[/green]")


def _clean_aws(config, cluster_name, yes):
    import boto3

    region = config.get("AWS_REGION", "us-east-1")

    if cluster_name:
        ec2 = boto3.client("ec2", region_name=region)
        paginator = ec2.get_paginator("describe_instances")
        pages = paginator.paginate(
            Filters=[
                {"Name": "tag:ray-cluster-name", "Values": [cluster_name]},
                {"Name": "instance-state-name", "Values": ["stopped"]},
            ]
        )
        ids = [
            inst["InstanceId"]
            for page in pages
            for res in page["Reservations"]
            for inst in res["Instances"]
        ]
        if not ids:
            console.print(f"[dim]No stopped instances for cluster '{cluster_name}' in {region}.[/dim]")
            return
        console.print(f"Found [bold]{len(ids)}[/bold] stopped instance(s) for cluster [cyan]{cluster_name}[/cyan].")
        if not yes and not click.confirm("Terminate?", default=True):
            return
        ec2.terminate_instances(InstanceIds=ids)
        console.print(f"[green]Terminated {len(ids)} instance(s).[/green]")
    else:
        ec2 = boto3.client("ec2", region_name=region)
        paginator = ec2.get_paginator("describe_instances")
        pages = paginator.paginate(
            Filters=[
                {"Name": "tag-key", "Values": ["ray-cluster-name"]},
                {"Name": "instance-state-name", "Values": ["stopped"]},
            ]
        )
        by_cluster = defaultdict(list)
        for page in pages:
            for res in page["Reservations"]:
                for inst in res["Instances"]:
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    name = tags.get("ray-cluster-name", "unknown")
                    by_cluster[name].append(inst["InstanceId"])

        if not by_cluster:
            console.print(f"[dim]No stopped Ray instances in {region}.[/dim]")
            return

        total = sum(len(ids) for ids in by_cluster.values())
        for name, ids in sorted(by_cluster.items()):
            console.print(f"  [cyan]{name}[/cyan]: {len(ids)} stopped instance(s)")
        console.print(f"\nTotal: [bold]{total}[/bold] instance(s).")
        if not yes and not click.confirm("Terminate all?", default=True):
            return
        all_ids = [i for ids in by_cluster.values() for i in ids]
        ec2.terminate_instances(InstanceIds=all_ids)
        console.print(f"[green]Terminated {len(all_ids)} instance(s).[/green]")


@click.command()
@click.argument("cluster")
def vscode(cluster):
    """Open VS Code on a running cluster via Remote SSH.

    Looks up the head node IP for CLUSTER, updates the SSH config entry,
    and launches VS Code. Use provider:name syntax for non-AWS (e.g. nebius:cpu).
    """
    from brr.aws.nodes import update_ssh_config

    config = read_config()

    provider, name, explicit = _resolve_provider(cluster)
    check_provider_configured(provider, config)
    project_root = _project_root_for(provider, name, explicit)
    cluster_name = _resolve_cluster_name(name, provider, project_root)

    if provider == "nebius":
        from brr.nebius.nodes import query_head_ip

        project_id = config.get("NEBIUS_PROJECT_ID", "")
        if not project_id:
            console.print("[red]NEBIUS_PROJECT_ID not configured. Run `brr configure nebius` first.[/red]")
            raise SystemExit(1)

        with console.status(f"[bold green]Looking up Nebius cluster '{cluster_name}'..."):
            head_ip = query_head_ip(project_id, cluster_name)

        if not head_ip:
            console.print(f"[red]No running Nebius cluster '{cluster_name}' found.[/red]")
            raise SystemExit(1)

        host = cluster_ssh_alias("nebius", cluster_name)
        update_ssh_config(host, head_ip, config.get("NEBIUS_SSH_KEY", ""))
    else:
        from brr.aws.nodes import query_ray_clusters

        region = config.get("AWS_REGION", "us-east-1")

        with console.status(f"[bold green]Looking up cluster '{cluster_name}' in {region}..."):
            clusters = query_ray_clusters(region)

        match = next(
            (c for c in clusters if c["cluster_name"] == cluster_name and c["state"] == "running"),
            None,
        )
        if not match or match["head_ip"] == "-":
            console.print(f"[red]No running cluster '{cluster_name}' found in {region}.[/red]")
            raise SystemExit(1)

        host = cluster_ssh_alias("aws", cluster_name)
        update_ssh_config(host, match["head_ip"], config.get("AWS_SSH_KEY", ""))

    remote_path = f"/home/ubuntu/code/{project_root.name}" if project_root else "/home/ubuntu/code"

    if shutil.which("code"):
        subprocess.run(["code", "--remote", f"ssh-remote+{host}", remote_path])
    else:
        console.print(
            f"[yellow]'code' command not found. "
            f"In VS Code: Cmd+Shift+P -> 'Shell Command: Install code command in PATH'[/yellow]"
        )
        console.print(f"[dim]Then run: code --remote ssh-remote+{host} {remote_path}[/dim]")


@click.command("list")
@click.option("-a", "--all", "show_all", is_flag=True, help="Show all clusters, not just this project's")
def list_cmd(show_all):
    """List live Ray clusters across configured providers.

    Inside a project, shows only clusters defined in .brr/ templates.
    Use --all to show every cluster.
    """
    config = read_config()

    project_root = find_project_root()

    all_clusters = []  # list of (provider, cluster_dict)
    ray_statuses = {}
    any_queried = False

    # Query AWS if configured
    if config.get("AWS_REGION"):
        any_queried = True
        from brr.aws.nodes import query_ray_clusters, get_ray_status

        region = config["AWS_REGION"]
        ssh_key = config.get("AWS_SSH_KEY", "")

        with console.status(f"[bold green]Querying AWS clusters in {region}..."):
            aws_clusters = query_ray_clusters(region)

        for c in aws_clusters:
            all_clusters.append(("aws", c))

        running = [c for c in aws_clusters if c["state"] == "running" and c["head_ip"] != "-"]
        if running and ssh_key:
            with console.status("[bold green]Querying Ray node status..."):
                for c in running:
                    ray_statuses[("aws", c["cluster_name"])] = get_ray_status(c["head_ip"], ssh_key)

    # Query Nebius if configured
    if config.get("NEBIUS_PROJECT_ID"):
        any_queried = True
        try:
            from brr.nebius.nodes import query_clusters

            nebius_ssh_key = config.get("NEBIUS_SSH_KEY", "")

            with console.status("[bold green]Querying Nebius clusters..."):
                nebius_clusters = query_clusters(config["NEBIUS_PROJECT_ID"])

            for c in nebius_clusters:
                all_clusters.append(("nebius", c))

            running = [c for c in nebius_clusters if c["state"] == "running" and c["head_ip"] != "-"]
            if running and nebius_ssh_key:
                from brr.aws.nodes import get_ray_status as _get_ray_status
                with console.status("[bold green]Querying Nebius Ray status..."):
                    for c in running:
                        ray_statuses[("nebius", c["cluster_name"])] = _get_ray_status(c["head_ip"], nebius_ssh_key)
        except (ImportError, ModuleNotFoundError):
            console.print("[dim]Nebius configured but SDK not installed (pip install brr\\[nebius])[/dim]")

    # Build project cluster map (always, for display purposes)
    cluster_map = {}  # cluster_name → template_stem
    if project_root:
        cluster_map = _project_cluster_map(project_root)

    # For --all, also scan staging YAMLs to identify projects for all clusters
    project_paths = {}  # cluster_name → project_path_str
    if show_all:
        project_paths = _staging_project_map()
        # Build cluster maps for all discovered projects (not just the current one)
        for proj_path in set(project_paths.values()):
            proj = Path(proj_path)
            if proj.is_dir() and proj_path != (str(project_root) if project_root else ""):
                cluster_map.update(_project_cluster_map(proj))

    # Filter to project clusters unless --all
    if not show_all:
        if project_root:
            all_clusters = [(p, c) for p, c in all_clusters if c["cluster_name"] in cluster_map]
        else:
            all_clusters = []

    if not all_clusters:
        if not any_queried:
            console.print("[yellow]No cloud provider configured.[/yellow]")
            console.print("Run [bold]brr configure[/bold] to set up AWS or Nebius.")
        elif not show_all:
            console.print("[dim]No clusters found for this project. Use --all to see all clusters.[/dim]")
        else:
            console.print("[dim]No Ray clusters found.[/dim]")
        return

    has_multiple_providers = len({p for p, _ in all_clusters}) > 1

    if project_root and not show_all:
        console.print(f"Project: [bold]{project_root.name}[/bold]")

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold white")
    if has_multiple_providers:
        table.add_column("Provider", style="dim")
    if show_all:
        table.add_column("Project")
    table.add_column("Cluster", style="cyan")
    table.add_column("State")
    table.add_column("Head IP", style="green")
    table.add_column("Instance Type")
    table.add_column("Nodes", justify="right")
    table.add_column("Uptime", justify="right")
    table.add_column("Resources")

    for provider, c in all_clusters:
        state = c["state"]
        if state == "running":
            state_str = "[green]running[/green]"
        elif state == "stopped":
            state_str = "[red]stopped[/red]"
        else:
            state_str = "[yellow]mixed[/yellow]"

        rs = ray_statuses.get((provider, c["cluster_name"]))
        if rs:
            parts = [f"{rs['cpu']} CPU"]
            if rs["gpu"]:
                parts.append(f"{rs['gpu']} GPU")
            resources = ", ".join(parts)
        else:
            resources = "-"

        # Show template name (e.g. "dev") instead of cluster_name when in project
        display_name = cluster_map.get(c["cluster_name"], c["cluster_name"])

        row = []
        if has_multiple_providers:
            row.append(provider)
        if show_all:
            row.append(project_paths.get(c["cluster_name"], ""))
        row.extend([
            display_name,
            state_str,
            c["head_ip"],
            c["instance_type"],
            str(c["node_count"]),
            c["uptime"],
            resources,
        ])
        table.add_row(*row)

    console.print(table)


@click.group()
def templates():
    """Template configuration commands."""


@templates.command("list")
def templates_list():
    """List available templates (project + built-in)."""
    project_root = find_project_root()

    if project_root:
        providers = find_project_providers(project_root)
        multi = len(providers) > 1
        for provider in providers:
            project_tpls = find_project_templates(project_root, provider)
            if project_tpls:
                label = f".brr/{provider}/" if multi else f".brr/{provider}/ (project)"
                console.print(f"[bold]{label}[/bold]")
                for name in project_tpls:
                    if multi:
                        console.print(f"  [bold cyan]{provider}:{name}[/bold cyan]")
                    else:
                        console.print(f"  [bold cyan]{name}[/bold cyan]")

    for provider in ("aws", "nebius"):
        builtin_tpls = list_templates(provider)
        if builtin_tpls:
            console.print(f"[bold]Built-in ({provider})[/bold]")
            for name in builtin_tpls:
                console.print(f"  [dim]{provider}:{name}[/dim]")


@templates.command()
@click.argument("template")
def show(template):
    """Show configurable arguments and rendered config for a template."""
    import yaml

    provider, tpl_name, explicit = _resolve_provider(template)
    project_root = _project_root_for(provider, tpl_name, explicit)

    cfg, _ = read_merged_config(project_root)
    check_provider_configured(provider, cfg)

    try:
        tpl_content, tpl_name = resolve_template(tpl_name, provider, project_root=project_root)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)

    rendered = render(tpl_content, cfg)
    template_aliases = extract_template_aliases(rendered)

    console.print(f"Template: [bold cyan]{template}[/bold cyan]")
    console.print()

    required_paths = find_required(rendered)
    if required_paths:
        reverse = {}
        for name, path in template_aliases.items():
            reverse[path] = name
        for name, info in GLOBAL_ARGS.items():
            if info["path"]:
                reverse[info["path"]] = name

        console.print("[bold]Required:[/bold]")
        for path in required_paths:
            friendly = reverse.get(path)
            if friendly:
                console.print(f"  [bold red]{friendly}[/bold red]  ({path})")
            else:
                console.print(f"  [bold red]{path}[/bold red]")
        console.print()

    non_required_aliases = {k: v for k, v in template_aliases.items()
                           if v not in required_paths}
    if non_required_aliases:
        console.print("[bold]Template overrides:[/bold]")
        for name, path in non_required_aliases.items():
            current = _get_nested(rendered, path)
            if current is not None:
                console.print(f"  {name:<24} \\[{current}]")
            else:
                console.print(f"  {name}")
        console.print()

    console.print("[bold]Common overrides:[/bold]")
    for name, info in GLOBAL_ARGS.items():
        path = info["path"]
        help_text = info["help"]
        if path:
            current = _get_nested(rendered, path)
            if current is not None:
                console.print(f"  {name:<24} {help_text} [dim]\\[{current}][/dim]")
            else:
                console.print(f"  {name:<24} {help_text}")
        else:
            console.print(f"  {name:<24} {help_text}")

    console.print()
    console.print("[dim]Any key=value using dot-notation also accepted.[/dim]")

    console.print()
    console.print("[bold]Rendered config:[/bold]")
    console.print(yaml.dump(rendered, default_flow_style=False, sort_keys=False), end="", highlight=False)
