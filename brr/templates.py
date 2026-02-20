import re
from importlib.resources import files
from pathlib import Path

import yaml

from brr.state import CONFIG_PATH, STATE_DIR, staging_dir_for, rendered_yaml_for

# Global args: friendly aliases available for all templates.
# "spot" has special handling (toggle InstanceMarketOptions).
GLOBAL_ARGS = {
    "instance_type": {
        "path": "available_node_types.ray.head.default.node_config.InstanceType",
        "help": "Head node instance type",
    },
    "max_workers": {"path": "max_workers", "help": "Maximum worker nodes"},
    "region": {"path": "provider.region", "help": "AWS region"},
    "az": {"path": "provider.availability_zone", "help": "Availability zone"},
    "ami": {
        "path": "available_node_types.ray.head.default.node_config.ImageId",
        "help": "Head node AMI",
    },
    "spot": {"path": None, "help": "Enable spot pricing (true/false)"},
    "capacity_reservation": {"path": None, "help": "Capacity reservation ID for capacity-block"},
}


def _template_dir(provider="aws"):
    """Return the template directory for a provider."""
    if provider == "aws":
        return files("brr.aws").joinpath("templates")
    return files(f"brr.{provider}").joinpath("templates")


def list_templates(provider="aws"):
    """List available built-in template names."""
    tpl_dir = _template_dir(provider)
    return sorted(
        p.stem
        for p in tpl_dir.iterdir()
        if p.name.endswith(".yaml")
    )


def find_project_templates(project_root, provider=None):
    """List template names from a project's .brr/{provider}/ directory.

    If provider is None, searches all provider subdirs.
    """
    brr_dir = Path(project_root) / ".brr"
    if not brr_dir.is_dir():
        return []
    if provider:
        pdir = brr_dir / provider
        if not pdir.is_dir():
            return []
        return sorted(p.stem for p in pdir.glob("*.yaml"))
    # All providers
    return sorted(p.stem for p in brr_dir.glob("*/*.yaml"))


def resolve_default_template(project_root, provider=None):
    """Pick the default template for a project.

    Returns the sole .yaml if only one exists, scoped to provider subdirectory if given.
    Raises click.UsageError if ambiguous.
    """
    import click

    templates = find_project_templates(project_root, provider)

    if not templates:
        raise click.UsageError(f"No templates found in {project_root}/.brr/")

    if len(templates) == 1:
        return templates[0]

    raise click.UsageError(
        f"Multiple templates in .brr/: {', '.join(templates)}. "
        "Specify one with `brr up <name>`."
    )


def resolve_template(name, provider="aws", project_root=None):
    """Find a template by name. Returns (template_content, template_name).

    Resolution:
    - File path (contains / or ends with .yaml) → read directly
    - With project_root → project template ONLY (.brr/{provider}/{name}.yaml)
    - Without project_root → built-in ONLY (brr/{provider}/templates/{name}.yaml)

    Use provider:name syntax (parsed before calling this) for built-in access.
    Bare names inside a project resolve project templates only.
    """
    # Direct file path
    if "/" in name or name.endswith(".yaml"):
        path = Path(name)
        if not path.exists():
            raise FileNotFoundError(f"Template file not found: {name}")
        return path.read_text(), path.stem

    # Project template (no fallthrough to built-in)
    if project_root is not None:
        project_tpl = Path(project_root) / ".brr" / provider / f"{name}.yaml"
        if project_tpl.exists():
            return project_tpl.read_text(), name
        available = find_project_templates(project_root, provider)
        raise FileNotFoundError(
            f"Project template '{name}' not found in .brr/{provider}/. "
            f"Available: {', '.join(available) if available else '(none)'}. "
            f"Use {provider}:{name} for built-in templates."
        )

    # Built-in template (no project)
    tpl_dir = _template_dir(provider)
    tpl_file = tpl_dir.joinpath(f"{name}.yaml")
    try:
        return tpl_file.read_text(), name
    except FileNotFoundError:
        available = [f"{provider}:{t}" for t in list_templates(provider)]
        raise FileNotFoundError(
            f"Built-in template '{provider}:{name}' not found. "
            f"Available: {', '.join(available)}"
        )


def render_placeholders(template_content, config):
    """Replace {{VAR}} placeholders with values from config dict. Returns string."""
    content = template_content
    for key, value in config.items():
        content = content.replace("{{" + key + "}}", value)
    return content


def render(template_content, config):
    """Render {{VAR}} placeholders, then parse as YAML. Returns dict."""
    rendered = render_placeholders(template_content, config)

    # Warn about unresolved placeholders
    remaining = re.findall(r"\{\{(\w+)\}\}", rendered)
    if remaining:
        from rich.console import Console
        Console().print(
            f"[yellow]Warning: unresolved placeholders: {remaining}[/yellow]"
        )

    return yaml.safe_load(rendered)


def _resolve_dotted_keys(root, path):
    """Resolve a dot-path against a nested dict, handling keys that contain dots.

    Greedily matches the longest existing key at each level.
    E.g. path 'available_node_types.ray.head.default.node_config.InstanceType'
    with dict key 'ray.head.default' resolves to:
    ['available_node_types', 'ray.head.default', 'node_config', 'InstanceType']
    """
    parts = path.split(".")
    keys = []
    d = root
    i = 0
    while i < len(parts):
        matched = False
        for j in range(len(parts), i, -1):
            candidate = ".".join(parts[i:j])
            if isinstance(d, dict) and candidate in d:
                keys.append(candidate)
                d = d[candidate]
                i = j
                matched = True
                break
        if not matched:
            keys.append(parts[i])
            d = d.get(parts[i]) if isinstance(d, dict) else None
            i += 1
    return keys


def _coerce_value(value):
    """Coerce a string value to bool/int/float if possible."""
    if not isinstance(value, str):
        return value
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _set_nested(d, path, value):
    """Set a value in a nested dict using dot-notation path.

    Handles YAML keys containing dots (e.g. 'ray.head.default') by
    greedily matching the longest existing key at each level.
    """
    keys = _resolve_dotted_keys(d, path)
    for key in keys[:-1]:
        if key not in d or not isinstance(d[key], dict):
            d[key] = {}
        d = d[key]
    d[keys[-1]] = _coerce_value(value)


def _get_nested(d, path):
    """Read a value from a nested dict using dot-notation path. Returns None if missing."""
    keys = _resolve_dotted_keys(d, path)
    for key in keys:
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


def extract_template_aliases(config):
    """Pop and return the _brr alias map from a parsed config. Strips it from the config."""
    return config.pop("_brr", {})


def _resolve_alias(key, template_aliases):
    """Resolve a key to a dot-path. Checks template aliases, then global args."""
    if template_aliases and key in template_aliases:
        return template_aliases[key]
    if key in GLOBAL_ARGS and GLOBAL_ARGS[key]["path"]:
        return GLOBAL_ARGS[key]["path"]
    return key


def find_required(config, prefix=""):
    """Recursively find all '???' values in config. Returns list of dot-paths."""
    paths = []
    for key, value in config.items():
        full_path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            paths.extend(find_required(value, full_path))
        elif value == "???":
            paths.append(full_path)
    return paths


def check_required(config, template_aliases=None):
    """Check for unresolved '???' values. Raises click.UsageError if any remain."""
    missing_paths = find_required(config)
    if not missing_paths:
        return

    # Build reverse lookup: dot-path → friendly name
    reverse = {}
    if template_aliases:
        for name, path in template_aliases.items():
            reverse[path] = name
    for name, info in GLOBAL_ARGS.items():
        if info["path"]:
            reverse[info["path"]] = name

    import click
    lines = []
    for path in missing_paths:
        friendly = reverse.get(path)
        if friendly:
            lines.append(f"  {friendly}  ({path})")
        else:
            lines.append(f"  {path}")

    raise click.UsageError(
        "Missing required arguments:\n" + "\n".join(lines)
    )


def apply_overrides(config, overrides, template_aliases=None):
    """Apply key=value overrides to parsed YAML dict. Returns modified dict.

    Resolution: template aliases → global args → raw dot-notation.
    Special handling: spot=true/false toggles InstanceMarketOptions.
    """
    for override in overrides:
        if "=" not in override:
            raise ValueError(f"Invalid override format: '{override}' (expected key=value)")
        key, value = override.split("=", 1)

        # Special: spot toggle
        if key == "spot":
            head_config = config["available_node_types"]["ray.head.default"]["node_config"]
            if value.lower() in ("true", "yes", "1"):
                head_config["InstanceMarketOptions"] = {"MarketType": "spot"}
            else:
                head_config.pop("InstanceMarketOptions", None)
            continue

        # Special: capacity reservation
        if key == "capacity_reservation":
            head_config = config["available_node_types"]["ray.head.default"]["node_config"]
            head_config["InstanceMarketOptions"] = {"MarketType": "capacity-block"}
            head_config["CapacityReservationSpecification"] = {
                "CapacityReservationTarget": {
                    "CapacityReservationId": value,
                }
            }
            continue

        # Resolve alias
        path = _resolve_alias(key, template_aliases)
        _set_nested(config, path, value)

    return config


def _read_global_setup(use_pkg=False):
    """Read the global setup script from ~/.brr/setup.sh, falling back to built-in."""
    global_setup = STATE_DIR / "setup.sh"
    if not use_pkg and global_setup.exists():
        return global_setup.read_text()
    pkg = files("brr.data")
    return pkg.joinpath("setup.sh").read_text()


def prepare_staging(name, provider="aws", project_root=None, use_pkg_setup=False):
    """Create staging directory with support files for a cluster.

    Two-layer setup:
      1. setup.sh (global) — from ~/.brr/setup.sh or built-in
      2. project-setup.sh (project) — from .brr/{provider}/setup.sh if it exists

    Also writes idle-shutdown.sh and config.env.
    Returns the staging directory path.
    """
    staging = staging_dir_for(name, provider)
    staging.mkdir(parents=True, exist_ok=True)
    staging.chmod(0o700)

    pkg = files("brr.data")

    # Layer 1: Global setup — from ~/.brr/setup.sh or built-in
    (staging / "setup.sh").write_text(_read_global_setup(use_pkg=use_pkg_setup))

    # Layer 2: Project setup — from .brr/{provider}/setup.sh if in a project
    project_setup_staged = False
    if project_root is not None:
        project_setup = Path(project_root) / ".brr" / provider / "setup.sh"
        if project_setup.exists():
            (staging / "project-setup.sh").write_text(project_setup.read_text())
            project_setup_staged = True

    # Remove stale project-setup.sh if not staging one this time
    if not project_setup_staged and (staging / "project-setup.sh").exists():
        (staging / "project-setup.sh").unlink()

    # Write idle-shutdown.sh and sync-repo.sh (always from built-in)
    (staging / "idle-shutdown.sh").write_text(pkg.joinpath("idle-shutdown.sh").read_text())
    (staging / "sync-repo.sh").write_text(pkg.joinpath("sync-repo.sh").read_text())

    # Copy config.env, injecting PROVIDER for setup.sh to branch on.
    config_text = ""
    if CONFIG_PATH.exists():
        config_text = CONFIG_PATH.read_text()
    if config_text and f'PROVIDER="' not in config_text:
        config_text = config_text.rstrip("\n") + f'\nPROVIDER="{provider}"\n'
    if config_text:
        (staging / "config.env").write_text(config_text)

    # For Nebius: copy the node provider module to staging so it's importable
    # on the head node for Ray's autoscaler
    if provider == "nebius":
        provider_lib = staging / "provider_lib" / "brr" / "nebius"
        provider_lib.mkdir(parents=True, exist_ok=True)
        (staging / "provider_lib" / "brr" / "__init__.py").touch()
        (provider_lib / "__init__.py").touch()
        src = Path(__file__).parent / "nebius" / "node_provider.py"
        (provider_lib / "node_provider.py").write_text(src.read_text())

        # Copy credentials so setup.sh can place them on the head
        creds = Path.home() / ".nebius" / "credentials.json"
        if creds.exists():
            (staging / "nebius_credentials.json").write_text(creds.read_text())

    # Copy GitHub SSH key if configured (delivered to nodes via file_mounts)
    from brr.state import read_config as _read_config
    config = _read_config()
    github_key = config.get("GITHUB_SSH_KEY", "")
    if github_key and Path(github_key).exists():
        (staging / "github_key").write_text(Path(github_key).read_text())
        (staging / "github_key").chmod(0o600)

    return staging


def inject_brr_infra(config, staging, git_info=None):
    """Inject file_mounts, initialization_commands, and setup_commands.

    Merges with any existing values from the template.
    If git_info is provided, sets up git clone of the project repo on the cluster.
    """
    # initialization_commands: ensure pip exists before Ray's internal setup
    config.setdefault("initialization_commands", [])
    config["initialization_commands"].insert(
        0, "export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a && sudo -E apt-get update -y && sudo -E apt-get install -y python3-pip"
    )

    # file_mounts: staging dir -> /tmp/brr/ on remote.  /tmp is always
    # user-writable so Ray's internal mkdir (no sudo) works fine.
    config.setdefault("file_mounts", {})
    config["file_mounts"]["/tmp/brr/"] = str(staging) + "/"

    # setup_commands: prepend our setup scripts (global, then project)
    config.setdefault("setup_commands", [])
    config["setup_commands"].insert(0, "bash /tmp/brr/setup.sh")
    if (staging / "project-setup.sh").exists():
        config["setup_commands"].insert(1, "bash /tmp/brr/project-setup.sh")

    # Ray expects these keys to exist (built-in providers fill them via
    # fillout_defaults, but external providers don't get that treatment)
    config.setdefault("head_setup_commands", [])
    config.setdefault("worker_setup_commands", [])
    config.setdefault("cluster_synced_files", [])

    # For external providers: embed SSH public key content into node_configs
    # so the NodeProvider can inject it via cloud-init. We embed the content
    # (not a file path) so it works both locally and on the head node.
    if config.get("provider", {}).get("type") == "external":
        private_key = config.get("auth", {}).get("ssh_private_key", "")
        if private_key:
            pub_key_path = private_key + ".pub"
            if Path(pub_key_path).exists():
                pub_key_content = Path(pub_key_path).read_text().strip()
                for nt in config.get("available_node_types", {}).values():
                    nc = nt.get("node_config", {})
                    nc.setdefault("ssh_public_key", pub_key_content)

    # Project repo sync: write git metadata to staging, clone via setup_command.
    if git_info is not None:
        import json
        (staging / "repo_info.json").write_text(json.dumps(git_info))
        config["head_setup_commands"].append("bash /tmp/brr/sync-repo.sh")

    return config




def write_yaml(config, output_path):
    """Write rendered config as YAML."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    return output_path


_NEBIUS_BAKE_FAMILIES = {
    "ubuntu22.04-driverless": "NEBIUS_IMAGE_CPU_BAKED",
    "ubuntu22.04-cuda12": "NEBIUS_IMAGE_GPU_BAKED",
}


def apply_baked_images(rendered, config):
    """Swap base images with baked images in rendered Ray YAML.

    AWS: replaces ImageId values with baked AMI IDs.
    Nebius: injects baked_image_id into node_config (node_provider uses it over image_family).
    """
    # AWS
    ami_map = {}
    for base, baked in [("AMI_UBUNTU", "AMI_UBUNTU_BAKED"), ("AMI_DL", "AMI_DL_BAKED")]:
        if config.get(baked):
            base_val = config.get(base, "")
            if base_val:
                ami_map[base_val] = config[baked]
    if ami_map:
        for node_type in rendered.get("available_node_types", {}).values():
            nc = node_type.get("node_config", {})
            if nc.get("ImageId") in ami_map:
                nc["ImageId"] = ami_map[nc["ImageId"]]

    # Nebius
    nebius_map = {}
    for family, config_key in _NEBIUS_BAKE_FAMILIES.items():
        baked_id = config.get(config_key, "")
        if baked_id:
            nebius_map[family] = baked_id
    if nebius_map:
        for node_type in rendered.get("available_node_types", {}).values():
            nc = node_type.get("node_config", {})
            family = nc.get("image_family", "")
            if family in nebius_map:
                nc["baked_image_id"] = nebius_map[family]


def global_setup_hash():
    """Return MD5 hex digest of the current global setup.sh content."""
    import hashlib
    content = _read_global_setup()
    return hashlib.md5(content.encode()).hexdigest()


def output_path_for(name, provider="aws"):
    """Return the rendered YAML output path for a cluster name."""
    return rendered_yaml_for(name, provider)
