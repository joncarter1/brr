# Changelog

## 0.12.1

### Fixed

- Validate Nebius `node_config` keys at `brr up` time — unknown keys (e.g. `premptible` instead of `preemptible`) now raise an immediate error instead of being silently ignored.

## 0.12.0

### Added

- Configurable idle shutdown on head nodes — `idle_shutdown_head` option in `_brr` template section controls whether the head node runs the idle-shutdown daemon (defaults to `true`, set to `false` to keep head nodes alive indefinitely).

### Fixed

- Skip restarting stopped preemptible Nebius instances — preempted instances likely lack capacity and `operation.wait()` would block indefinitely. Creates fresh instances instead.
- Add apt retry config (3 retries) to setup.sh to handle transient package mirror timeouts during node setup.

## 0.11.4

### Fixed

- Fix PYTHONPATH not reaching `ray start` on Nebius clusters — Ray's SSH multiplexing (`ControlMaster`) bypasses PAM on subsequent commands, so `/etc/environment` written during setup is invisible to start commands. Now inlines `export PYTHONPATH` directly on `ray start` command lines.

### Added

- Support `spot=true` override for Nebius clusters — creates preemptible instances with stop-on-preemption policy.

## 0.11.3

### Fixed

- Fix Nebius node provider not importable on cached node restarts — `/tmp` is cleared on reboot (losing provider_lib and the .pth file), and the `PYTHONPATH` export in `start_ray_commands` didn't persist across SSH sessions. Now installs provider_lib to `/opt/brr` and sets `PYTHONPATH` via `/etc/environment`.

## 0.11.2

### Fixed

- Fix `brr-ensure-mount` failing on cached Nebius node restarts — `/tmp/brr/config.env` doesn't survive reboot, so detect the shared filesystem from fstab instead. Also actively retries mount for virtiofs entries that don't auto-mount on reboot.

## 0.11.1

### Fixed

- Warn when project template contains `cluster_name` — this field is ignored (auto-derived from project + template name) and can be safely removed.

## 0.11.0

### Fixed

- Attach security group to restarted Nebius nodes — stopped nodes from before the SG feature were restarted without the security group, leaving Ray ports exposed.
- Skip comment lines when warning about unresolved template placeholders — comments containing `{{VAR}}` examples no longer trigger false warnings.

## 0.10.1

### Fixed

- Always reuse stopped Nebius nodes on `brr up` — previously, nodes stopped by idle-shutdown were orphaned because reuse was gated behind `cache_stopped_nodes`. Now reuse runs unconditionally, and excess stopped nodes are cleaned up when not caching.

## 0.10.0

### Added

- Ray token authentication for cluster security — clusters now require a token to connect to the Ray dashboard and API.
- Nebius security groups — `brr configure nebius` creates a security group that blocks public access to Ray ports, and `brr up` validates it's present.

### Fixed

- Auto-source `config.env` in project setup scripts so variables like `PROVIDER` are available (previously caused `unbound variable` errors with `set -u`).
- Fix Nebius network egress rules for security groups.

## 0.9.1

### Fixed

- Install uv directly to `~/.local/lib/` via `UV_INSTALL_DIR` instead of swapping the binary after install. Eliminates fragile binary-move logic and makes `uv self update` safe (updates the binary without overwriting the wrapper).
- Enable model invocation for the `/release` skill.

## 0.9.0

### Added

- Auto-configure Nebius Object Storage credentials on instances — `brr configure nebius` creates S3 access keys and `setup.sh` configures AWS CLI automatically.
- Service account setup is now a required step in `brr configure nebius`.
- Attach service account to Nebius instances for Object Storage access.
- Auto-derive `cluster_name` from project directory name — no more manual naming.
- Clean up boot disks when deleting Nebius instances.

### Changed

- Built-in templates now require `--no-project` when run inside a project directory.
- `brr list` returns early with a helpful message when not in a project instead of silently showing nothing.
- S3 secret keys are stripped from baked images.

## 0.8.3

### Fixed

- Fix `brr-ensure-mount` hanging on Nebius when no shared filesystem is configured — a non-empty AWS `EFS_ID` in config.env prevented the early exit check.
- Strip legacy `head_node` and `worker_nodes` fields from Ray config to silence Ray 2.x warnings.

## 0.8.2

### Fixed

- Specify explicit CPU/GPU resources in all AWS Ray YAML templates instead of relying on auto-discovery.
- Default `cache_stopped_nodes` to `False` in all AWS templates to avoid bugs with cached stopped instances.

## 0.8.1

### Fixed

- Add `IamInstanceProfile` (`ray-autoscaler-v1`) to all AWS template node configs so instances get the correct IAM role.

## 0.8.0

### Changed

- `brr up` and `brr init` no longer inject `ray[default]` and cloud SDKs via `--with` — projects must add them as dependencies (e.g. `uv add 'ray[default]' boto3`).
- `brr init` prints `uv add` instructions after scaffolding.
- `brr up` shows a helpful error if ray is missing from project dependencies.

## 0.7.0

### Fixed

- Autoscaling broken when multiple clusters share the same filesystem — `ray_bootstrap_config.yaml` on the shared home directory caused cross-cluster config contamination. Autoscaler now reads from instance-local `/tmp/ray_bootstrap_config.yaml`.
- Rich markup not escaped in ray command output.

## 0.6.0

### Changed

- boto3, nebius, and ray[default] are now core dependencies — install with `uv tool install brr-cli` (no extras needed).
- Removed `[project.optional-dependencies]` (aws/nebius extras).
- `brr up` now uses `uv run --with` to inject ray and cloud SDK when running inside a uv project.

## 0.5.0

### Fixed

- Shared filesystem mount lost on cached node restarts — added `brr-ensure-mount` helper that re-establishes the home bind-mount.

### Changed

- All template commands (`up`, `down`, `attach`, etc.) now require explicit `provider:name` prefix — bare names like `brr up dev` no longer work.
- PyPI publish workflow now triggers on tag push instead of GitHub release.

## 0.4.2

### Fixed

- Idle shutdown daemon never triggering when tmux is running — `get_ssh_sessions()` counted tmux panes as SSH sessions. Now uses `ss` to count actual TCP connections on port 22.
- `brr up aws:cluster` printing `brr attach cluster` instead of `brr attach aws:cluster` — provider prefix was dropped from connect messages.
- AWS staging paths not namespaced by provider — now all providers use `~/.brr/staging/{provider}/{name}` consistently.

## 0.4.1

### Fixed

- `uv pip` commands (show, install, list) not targeting the project venv on cluster nodes. The uv wrapper now also sets `VIRTUAL_ENV` when a project venv exists.

## 0.4.0

### Added

- Update notice — brr now checks PyPI once per day and prints a notice when a newer version is available.
- Template documentation (`docs/templates.md`) and inline docs in all YAML template files.
- Pre-installed Python versions (3.10–3.13) for faster autoscaler node setup.

### Changed

- Global `setup.sh` is now always the built-in package version. It updates automatically with `uv tool upgrade brr-cli`. The `~/.brr/setup.sh` copy is no longer created or used. Removed `--dev-setup` flag.

### Fixed

- `brr up --dry-run` showing unrendered `{{VAR}}` placeholders instead of resolved values.
- `sync-repo.sh` incorrectly running on worker nodes (now head-only).

## 0.3.0

### Added

- Git-based project sync — `brr up` in a git repo clones the project on cluster nodes and keeps it in sync.
- Persistent home directory — `$HOME` is bind-mounted from EFS/virtiofs so all state survives instance replacement.
- `brr clean` works across all configured providers (was AWS-only).
- `brr nuke` redesigned with parallel multi-region cleanup and confirmation prompts.

### Changed

- GitHub SSH keys no longer use AWS Secrets Manager. Config key `GITHUB_SSH_SECRET` replaced by `GITHUB_SSH_KEY` — re-run `brr configure aws` to update.
- Removed project-level `.brr/config.env` — all config is now global (`~/.brr/config.env` only).
- Removed `DEFAULT_PROVIDER` and `DEFAULT_TEMPLATE` config keys (use explicit `provider:name` prefix instead).

### Added

- `--no-project` flag for `brr up` to skip `.brr/` project auto-discovery.

### Fixed

- uv lock file failures on EFS — caches and Python installs routed to `/tmp`.
- `brr up` now detects uv-managed projects and runs Ray via `uv run --group brr ray` automatically.
- Broken `brrray` group name in project templates — was concatenating `brr` and `ray` without a space.
- `brr attach` now opens an interactive login shell (`bash -lic`) so `.bashrc` aliases work.
- `sync-repo.sh` runs `uv sync --group brr` after syncing so Ray commands are immediately available.

## 0.2.0

### Added

- Resizable shared filesystem — `brr configure nebius` now lets you upsize an existing shared filesystem.
- Configurable disk type via `disk_type` in Nebius node_config (e.g. `network-ssd`, `network-ssd-nonreplicated`).
- `cache_stopped_nodes` provider config option for Nebius — opt-in to stop-instead-of-delete on scale-down.
- Node caching documentation in README.

### Changed

- Nebius nodes are now **deleted** on scale-down by default (was stop). Stopped instances still incur disk charges. Enable `cache_stopped_nodes: true` to keep the old behavior.
- Nebius default templates now use 50 GB boot disks (down from 100-500 GB).
- SSH aliases now consistently use `brr-{provider}-{name}` (e.g. `brr-aws-h100`).
- `brr nuke --provider nebius` no longer removes AWS SSH config entries (and vice versa).
- `brr --version` now reads from package metadata instead of a hardcoded string.

### Fixed

- `brr configure nebius` no longer lets you attempt to shrink a filesystem (Nebius only supports growing).

## 0.1.0

### Added

- Cluster lifecycle commands: `brr up`, `brr down`, `brr attach`, `brr list`, `brr clean`, `brr vscode`.
- AWS provider with templates: `cpu`, `l4`, `h100`, `cpu-l4`.
- Nebius provider with templates: `cpu`, `h100`, `cpu-h100`.
- Interactive setup wizards: `brr configure aws`, `brr configure nebius`, `brr configure tools`.
- Project-based workflows: `brr init` scaffolds `.brr/` with per-repo templates and setup scripts.
- Template override system with inline args (e.g. `brr up aws:h100 spot=true`).
- Shared filesystem support: EFS (AWS), virtiofs (Nebius).
- AI coding tool installation: Claude Code, Codex, Gemini CLI.
- Dotfiles integration via GNU Stow.
- Idle shutdown daemon monitoring CPU, GPU, and SSH activity.
- Image baking: `brr bake aws` for pre-built AMIs.
- Shell completions: `brr completion bash/zsh/fish`.
- `brr nuke` for full cloud resource teardown.
- `brr config` for managing `config.env` settings.
