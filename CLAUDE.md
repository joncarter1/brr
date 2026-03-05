# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Setup

```bash
# Install dependencies (uses uv + hatchling)
uv sync

# Install CLI in editable mode
uv tool install --editable .

# Run the CLI
brr --help
```

There are no tests, linters, or CI pipelines configured.

## Architecture

brr is a CLI for managing GPU/CPU compute clusters across AWS and Nebius. It uses Click for commands, Rich for terminal output, InquirerPy for interactive prompts, PyYAML for config templating, and Ray for cluster orchestration.

### Command Flow

All cluster commands (`up`, `down`, `attach`, `list`, `clean`, `vscode`) live in `brr/cluster.py` and follow this pattern:

1. **Provider parsing** — `state.py:parse_provider()` splits `provider:name` syntax (e.g. `nebius:h100`). Default provider is `aws`.
2. **Config loading** — `state.py:read_merged_config()` layers: `CONFIG_DEFAULTS` → `~/.brr/config.env`.
3. **Template resolution** — `templates.py:resolve_template()` finds a YAML: project templates (`.brr/{provider}/{name}.yaml`) take precedence when inside a project; explicit `provider:name` prefix bypasses project and uses built-in (`brr/{provider}/templates/{name}.yaml`).
4. **Rendering** — `{{VAR}}` placeholders replaced with config values; `???` marks required fields that must be overridden.
5. **Overrides** — CLI args like `instance_type=t3.xlarge` applied via alias system (`_brr` YAML section), `GLOBAL_ARGS` mapping, or raw dot-notation paths.
6. **Staging** — `prepare_staging()` writes setup scripts and config to `~/.brr/staging/{name}/`, then `inject_brr_infra()` adds file_mounts and setup_commands to the Ray YAML.
7. **Execution** — `ray up`/`ray down` called via subprocess.
8. **SSH config sync** — `nodes.py:update_ssh_config()` writes `brr-{cluster}` host entries to `~/.ssh/config`.

### Project System

Projects are repos with a `.brr/` directory (created by `brr init`):

```
.brr/
  setup.sh             # Project deps — runs after global setup on every node
  aws/dev.yaml         # Project template (standard Ray YAML)
  aws/cluster.yaml
```

Key behaviors:
- `state.py:find_project_root()` walks up from CWD looking for `.brr/` with YAML files (skips `~/.brr`).
- `resolve_project_provider()` infers provider from project: single provider → automatic; multiple → requires explicit prefix.
- Setup layering: built-in global `setup.sh` runs first, then project `.brr/setup.sh`.
- uv-managed projects: `brr init` writes `uv run ray start` into project template YAML. Projects must add `ray[default]` and their cloud SDK (e.g. `boto3`) as dependencies.

### Key Modules

- **`brr/cli.py`** — Click command group, version from `importlib.metadata`.
- **`brr/cluster.py`** — Cluster lifecycle. Uses `_find_ray()` to locate the Ray binary and `_run_ray()` to exec it.
- **`brr/state.py`** — Config parsing (`read_config`/`write_config`), state dirs, project discovery, provider checks.
- **`brr/templates.py`** — Template resolution, rendering, override system, staging, baked image substitution.
- **`brr/commands/init.py`** — `brr init` scaffolds `.brr/{provider}/` with templates + `.brr/setup.sh`. Maps project template names to built-in ones (`_TEMPLATE_MAP`).
- **`brr/commands/configure.py`** — Interactive wizard: cloud provider, AI tools, general settings. Uses InquirerPy for menus.
- **`brr/commands/bake.py`** — Pre-bakes global setup into AMIs/images. Strips secrets (`_BAKE_STRIP_KEYS`) before baking. Tracks staleness via setup.sh hash.
- **`brr/commands/nuke.py`** — Destructive teardown. Multi-region parallel cleanup with ThreadPoolExecutor (AWS) or async SDK (Nebius).
- **`brr/data/setup.sh`** — Node bootstrap: mounts, AWS CLI, GitHub SSH keys, AI tools, Python venv, Ray, idle shutdown daemon.
- **`brr/data/idle-shutdown.sh`** — Systemd daemon monitoring CPU/GPU/SSH activity.

#### AWS

- **`brr/aws/configure.py`** — Creates key pairs, security groups, EFS, registers GitHub SSH keys.
- **`brr/aws/nodes.py`** — EC2 queries (`query_ray_clusters`), SSH config management.
- **`brr/aws/templates/`** — Ray YAML templates: `cpu.yaml`, `l4.yaml`, `h100.yaml`, `cpu-l4.yaml`.

#### Nebius

- **`brr/nebius/configure.py`** — Project selection, subnet, SSH keys, shared filesystem, GitHub SSH.
- **`brr/nebius/nodes.py`** — Instance queries (`query_clusters`, `query_head_ip`), SSH config management.
- **`brr/nebius/node_provider.py`** — Custom Ray NodeProvider for autoscaling. Stop-instead-of-delete for cached nodes. Restarts stopped instances before creating new ones.
- **`brr/nebius/templates/`** — Ray YAML templates: `cpu.yaml`, `h100.yaml`, `cpu-h100s.yaml`.

### Known Pitfalls

- `textwrap.dedent` with f-strings breaks when interpolated values have different indentation. Build shell scripts as concatenated string parts instead.
- `config.env` keys can contain digits (e.g. `NEBIUS_IMAGE_GPU_BAKED`), so the parsing regex must be `[A-Z0-9_]+`.
- Nebius `recovery_policy` is immutable after instance creation — must use `InstanceRecoveryPolicy.FAIL` to prevent auto-restart after idle shutdown.
- Nebius instance state 8 is ERROR (not DELETED) — `_TERMINAL_STATES` must include it.
- External providers (Nebius) need explicit `resources: {CPU: N}` in Ray YAML templates — Ray can't auto-detect.
- Unresolved `{{VAR}}` placeholders in provider config must be guarded (e.g. `"{{" in value` check in node_provider).
- InquirerPy `Choice` class doesn't have a `disabled` parameter. Use dict syntax `{"value": ..., "name": ..., "disabled": "reason"}` for disabled items.
