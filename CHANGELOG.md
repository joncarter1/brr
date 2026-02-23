# Changelog

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
