# Changelog

## 0.2.0

### Added

- Resizable shared filesystem — `brr configure nebius` now lets you upsize an existing shared filesystem.
- Configurable disk type via `disk_type` in Nebius node_config (e.g. `network-ssd`, `network-ssd-nonreplicated`).
- `cache_stopped_nodes` provider config option for Nebius — opt-in to stop-instead-of-delete on scale-down.
- Node caching documentation in README.

### Changed

- Nebius nodes are now **deleted** on scale-down by default (was stop). Stopped instances still incur disk charges. Enable `cache_stopped_nodes: true` to keep the old behavior.
- Nebius default templates now use 50 GB boot disks (down from 100-500 GB).
- Nebius default templates now use `network-ssd-nonreplicated` (25% cheaper than `network-ssd`).
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
