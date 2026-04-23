# Changelog

## 0.15.0

### Added

- **Multi-region Nebius.** Configure any number of Nebius regions via `NEBIUS_REGIONS=eu-north1,us-central1,...` and per-region keys `NEBIUS_{REGION}_PROJECT_ID` / `_SUBNET_ID` / `_SECURITY_GROUP_ID` / `_FILESYSTEM_ID` / `_SERVICE_ACCOUNT_ID` / `_S3_ACCESS_KEY_ID` / `_S3_SECRET_KEY`. Flat single-region configs (`NEBIUS_PROJECT_ID`, …) still work unchanged.
- `region=X` override on `brr up` / `attach` / `down` / `vscode` for Nebius; cluster names get a `-{region}` suffix when 2+ regions are configured so names don't collide. Region is recorded in each cluster's staging `brr_meta.json` for O(1) resolution on subsequent commands.
- `brr list` and `brr nuke` iterate configured Nebius regions in parallel; `nuke` prompts which region(s) to target when more than one is configured.
- New `brr/nebius/templates/h200.yaml` — single-node H200 GPU dev box (`gpu-h200-sxm`, `1gpu-16vcpu-200gb`).

### Fixed

- Nebius autoscaler now deletes orphan-stopped instances (preempted workers, idle-shutdown, or console-stopped) when `cache_stopped_nodes` is false. Previously these lingered indefinitely, accruing disk costs.
- Service account key generation bumped from 2048-bit to 4096-bit RSA (Nebius IAM now requires 4096).
- Configure wizard rebuilds the Nebius SDK per async block instead of caching it — a cached SDK binds its gRPC channel to the first event loop it sees and crashes on the next `asyncio.run()` with "Event loop is closed".

## 0.14.2

### Added

- **Verda provider (beta).** New `verda` cloud provider alongside AWS and Nebius. Ships a Ray NodeProvider with stop-reuse caching, configure wizard, and built-in templates for `verda:h100` (1× H100 SXM5 in FIN-02), `verda:a100` (1× A100 80GB in FIN-01), and `verda:cpu` (single-node CPU dev box).
- **Known limitation:** Verda has no cloud-level firewall. `setup.sh` locks down Verda instances with `ufw` (default deny inbound, SSH + loopback only). Multi-node Verda clusters are not yet supported — workers can't reach the head's Ray ports through the ufw allowlist. The `cpu` template is single-node only; a per-cluster worker-IP allowlist mechanism is planned.
- Per-provider SSH user via `Provider.ssh_user()` — Verda uses `root`; AWS and Nebius still default to `ubuntu`.
- Dedicated `brr/github.py` module with `ensure_github_key()`. A single `~/.brr/keys/github-*` key is now shared across all providers, decoupled from each provider's cluster SSH key.
- Idle-shutdown daemon now also checks network throughput (rx+tx KB/s) alongside CPU and SSH activity.

### Fixed

- `initialization_commands` waits for `cloud-init` and uses `DPkg::Lock::Timeout=300` so fresh VMs no longer race `unattended-upgrades` on first boot.
- `ssh.py:update_ssh_config` purges stale `known_hosts` entries for the target IP before writing a new block, so recycled cloud IPs don't trigger strict-checking failures on manual `ssh brr-*` connects.

### Changed

- GitHub SSH key is no longer tied to any provider's cluster key. Whichever provider you configure first generates the shared key; subsequent wizards reuse it instead of overwriting `GITHUB_SSH_KEY`. Pre-existing `brr-aws` / `brr-nebius` / `brr-verda` entries on GitHub remain until removed manually or by `brr nuke`.
- `brr nuke` now deletes every GitHub SSH key entry whose title starts with `brr`, via the new shared helper.

## 0.14.1

### Fixed

- Nebius node_provider: stopped-node reuse now matches on `ray-user-node-type` (was matching on the `ray-node-type` kind), so one worker type cannot be restarted and re-tagged as another.
- Nebius node_provider: run the SDK event loop on a dedicated thread so a slow `CreateInstance` no longer freezes the autoscaler. In-flight create/restart targets are hidden from `non_terminated_nodes` and `_set_node_tags` retries on `FAILED_PRECONDITION` to avoid racing Nebius "active operation" rejection.
- `brr list`: wrap Nebius queries in a 30s timeout and surface errors as a yellow status line instead of aborting the whole listing.

## 0.14.0

### Changed

- AWS templates now hardcode AMI IDs directly in the built-in YAMLs (`ami-05e86b3611c60b0b4` for Ubuntu 22.04, `ami-011f3ba065b22345e` for CUDA 13 DLAMI, both `us-east-1`). Override by editing the template's `ImageId:` line. `AMI_UBUNTU` / `AMI_DL` removed from `brr configure aws` and `DEFAULTS`.
- Bake subsystem removed: `brr bake aws` / `brr bake nebius` commands, `apply_baked_images`, provider `bake_hint` methods, Nebius `baked_image_id` node_config key, and `BAKE_SETUP_HASH` tracking. To be revisited later.

## 0.13.5

### Fixed

- Ray skips `file_mounts` rsync and `setup_commands` when its content hashes match, leaving `/tmp/brr/staging/` and `/tmp/ray_bootstrap_config.yaml` missing on re-deploy. A timestamp `.sync_marker` is now written into staging to force Ray to re-sync, and `ray_bootstrap_config.yaml` is copied to `/tmp` in `head_start_ray_commands` (which always runs) instead of only in `setup.sh`.
- Multi-cluster setups sharing an EFS/virtiofs filesystem could clobber each other's `ray_bootstrap_config.yaml` in `$HOME`. `setup.sh` now symlinks `~/ray_bootstrap_config.yaml` to instance-local `/tmp/ray_bootstrap_config.yaml`.

## 0.13.4

### Fixed

- Always disable Ray config cache on `brr up` so staging files are re-synced to nodes on restart. Previously, `brr down` then `brr up` on clusters with `cache_stopped_nodes` left `/tmp/brr/staging/` empty because Ray skipped file_mounts and setup_commands.
- Re-sync staging from `/tmp` to `/opt` in `start_ray_commands` as a fallback for cached worker node restarts where `setup_commands` are skipped.

## 0.13.3

### Fixed

- Fix `Permission denied` creating `/opt/brr/staging` during cluster setup. Ray runs an unprivileged `mkdir` before rsync, so `file_mounts` now targets `/tmp/brr/staging/` and `setup_commands` copies to `/opt/brr/staging/` with sudo.

## 0.13.2

### Fixed

- Move staging file paths from `/tmp/brr/` to `/opt/brr/staging/` in bake commands for consistency with cluster setup.
- Fix `idle_shutdown_head=false` and `_brr.*` CLI overrides leaking into Ray YAML and causing validation errors.

### Added

- `head_disk_gb` CLI override to set head node disk size (e.g. `brr up cpu head_disk_gb=4096`).

## 0.13.1

### Fixed

- Skip creating Python symlinks in `~/.local/bin` during `uv python install` (`--no-bin`) — avoids conflicts with brr's custom python wrappers.
- Remove `_BRR_ENV_LOADED` source guard from `/etc/profile.d/brr.sh` so environment variables update on re-source after `brr up`.

## 0.13.0

### Fixed

- Move Python venv and uv-managed interpreters from `/tmp` to `/opt` so they survive reboots — fixes cached node restarts where Ray skips `setup_commands` but `/tmp` is cleared.

## 0.12.2

### Fixed

- Bump Nebius CPU template boot disk size from 50GB to 100GB.

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
