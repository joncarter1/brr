# ❄️ brr ❄️

Opinionated research infrastructure tooling. Launch clusters, get SSH access, start building.

## Features
- **Shared filesystem** — All nodes share `~/code/` via EFS (AWS) or virtiofs (Nebius).
- **Coding tools** — Install Claude Code, Codex, or Gemini. Connect with e.g. `brr attach dev claude`
- **Autoscaling** — Ray-based cluster scaling with cached instances.
- **Project-based workflows** — Per-repo cluster configs and project-specific dependencies.
- **Auto-shutdown** — Monitors CPU, GPU, and SSH activity. Shuts down idle instances to save costs.
- **Dotfiles integration** — Take your dev environment (vim, tmux, shell config) to every cluster node via GNU Stow.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (for installation)

## Quick Start

```sh
# Install (AWS only)
uv tool install brr-cli[aws]

# Install (both providers)
# uv tool install brr-cli[aws,nebius] 

# Configure (interactive wizard)
brr configure      # or: brr configure nebius

# Launch an H100
brr up aws:h100

# brr up nebius:h100

# Connect
brr attach aws:h100              # SSH
brr attach aws:h100 claude       # Claude Code on the cluster
brr vscode aws:h100              # VS Code remote
```

Built-in templates use `provider:name` syntax (e.g. `aws:h100`). Inside a [project](#projects), short names like `brr up dev` work automatically.

Supported clouds: [AWS](#aws-setup) · [Nebius](#nebius-setup)

## Projects

For per-repo cluster configs, initialize a project:

```sh
cd my-research-repo/
brr init
```

This creates:

```
.brr/
  aws/
    dev.yaml        # Single GPU for development
    cluster.yaml    # CPU head + GPU workers
    setup.sh        # Project-specific dependencies
  config.env        # Project config (overrides global)
```

Templates are standard Ray YAML — edit them or add your own. Inside a project, use short names:

```sh
brr up                  # launches DEFAULT_TEMPLATE (set in .brr/config.env)
brr up dev              # launches .brr/aws/dev.yaml
brr up cluster          # launches .brr/aws/cluster.yaml
brr attach dev          # SSH into dev cluster
brr down dev            # tear down
```

If your project uses `uv`, `brr init` automatically adds `brr-cli` and `ray` to a `brr` dependency group. The cluster uses your project-locked versions — no manual setup needed.

Project config (`.brr/config.env`) overrides global settings (`~/.brr/config.env`). Use it for project-specific settings like idle timeouts or dotfiles.

## Templates

### Built-in templates

| Template | Instance | GPU | Workers |
| :--- | :--- | :--- | :--- |
| `aws:cpu` | t3.2xlarge | — | 0-2 |
| `aws:l4` | gr6.4xlarge | 1x L4 | — |
| `aws:h100` | p5.4xlarge | 8x H100 | — |
| `aws:cpu-l4` | t3.2xlarge + g6.4xlarge | 1x L4 | 0-4 |
| `nebius:cpu` | 8vcpu-32gb | — | 0-2 |
| `nebius:h100` | 1gpu-16vcpu-200gb | 1x H100 | — |
| `nebius:cpu-h100` | 8vcpu-32gb + 8gpu-128vcpu-1600gb | 8x H100 | 0-4 |

### Overrides

Override template values inline:

```sh
brr up aws:cpu instance_type=t3.xlarge max_workers=4
brr up aws:h100 spot=true
brr up dev region=us-west-2
```

Preview the rendered config without launching:

```sh
brr up dev --dry-run
```

See available overrides for a template:

```sh
brr templates show dev
```

### Multi-provider

Use the provider prefix for built-in templates:

```sh
brr up aws:h100
brr up nebius:h100
brr attach nebius:h100
brr down nebius:h100
```

Both providers can run simultaneously.

## Customization

### Node setup

`~/.brr/setup.sh` runs on every node boot. It installs packages, mounts shared storage, sets up Python/Ray, GitHub SSH keys, AI coding tools, dotfiles, and the idle shutdown daemon.

Edit it to customize:
```sh
vim ~/.brr/setup.sh
```

Project-specific dependencies go in `.brr/{provider}/setup.sh` (created by `brr init`), which runs after the global setup.

### AI coding tools

Install AI coding assistants on every cluster node:

```sh
brr configure tools    # select Claude Code, Codex, and/or Gemini CLI
```

Then connect and start coding:

```sh
brr attach dev claude
```

### Dotfiles

Set a dotfiles repo to sync your dev environment to every node:

```sh
brr config set DOTFILES_REPO "https://github.com/user/dotfiles"
```

The repo is cloned to `~/dotfiles` and installed via `install.sh` (if present) or GNU Stow.

### Image baking

Bake the global setup into AMIs/images for fast boot:

```sh
brr bake aws          # bake both CPU + GPU AMIs
brr bake status       # check if baked images are up to date
```

After baking, clusters boot from the pre-built image. Only project-specific deps need to install. `brr up` warns when `setup.sh` has changed since the last bake.

### Idle shutdown

A systemd daemon monitors CPU, GPU, and SSH activity. When all signals are idle for the configured timeout, the instance shuts down.

Configure in `~/.brr/config.env`:

```
IDLE_SHUTDOWN_ENABLED="true"
IDLE_SHUTDOWN_TIMEOUT_MIN="30"
IDLE_SHUTDOWN_CPU_THRESHOLD="10"
IDLE_SHUTDOWN_GRACE_MIN="15"
```

The grace period prevents shutdown during initial setup. Monitor on a node with `journalctl -u idle-shutdown -f`.

## Commands

| Command | Description |
| :--- | :--- |
| `brr up TEMPLATE [OVERRIDES...]` | Launch or update a cluster (`aws:h100`, `dev`, or `path.yaml`) |
| `brr up TEMPLATE --dry-run` | Preview rendered config without launching |
| `brr down TEMPLATE` | Stop a cluster (instances preserved for fast restart) |
| `brr down TEMPLATE --delete` | Terminate all instances and remove staging files |
| `brr attach TEMPLATE [COMMAND]` | SSH into head node, optionally run a command (e.g. `claude`) |
| `brr list [--all]` | List clusters (project-scoped by default, `--all` for everything) |
| `brr clean [TEMPLATE]` | Terminate stopped (cached) instances |
| `brr vscode TEMPLATE` | Open VS Code on a running cluster |
| `brr templates list` | List built-in templates |
| `brr templates show TEMPLATE` | Show template config and overrides |
| `brr init` | Initialize a project (interactive provider selection) |
| `brr configure [cloud\|tools\|general]` | Interactive setup (cloud provider, AI tools, settings) |
| `brr config [list\|get\|set\|path]` | View and manage configuration |
| `brr bake [aws\|nebius]` | Bake setup into cloud images |
| `brr bake status` | Check if baked images are up to date |
| `brr completion [bash\|zsh\|fish]` | Shell completion (`--install` to add to shell rc) |
| `brr nuke [aws\|nebius]` | Tear down all cloud resources |

## Cloud Setup

### AWS Setup

1. Attach the [IAM policy](brr/aws/iam-policy.json) to your IAM user
2. Install the [AWS CLI](https://aws.amazon.com/cli/) and run `aws configure`
3. *(Optional)* For GitHub SSH access on clusters, authenticate the [GitHub CLI](https://cli.github.com/):
   ```sh
   gh auth login
   gh auth refresh -h github.com -s admin:public_key
   ```
4. Run the setup wizard:
   ```sh
   brr configure aws
   ```

### Nebius Setup

1. Install the [Nebius CLI](https://docs.nebius.com/cli/install) and run `nebius init`
2. Create a service account with editor permissions:
   ```sh
   TENANT_ID="<your-tenant-id>"  # from console.nebius.com → Administration

   SA_ID=$(nebius iam service-account create \
     --name brr-cluster --format json | jq -r '.metadata.id')

   EDITORS_GROUP_ID=$(nebius iam group get-by-name \
     --name editors --parent-id $TENANT_ID --format json | jq -r '.metadata.id')

   nebius iam group-membership create \
     --parent-id $EDITORS_GROUP_ID --member-id $SA_ID
   ```
3. Generate credentials:
   ```sh
   mkdir -p ~/.nebius
   nebius iam auth-public-key generate \
     --service-account-id $SA_ID --output ~/.nebius/credentials.json
   ```
4. Run the setup wizard:
   ```sh
   brr configure nebius
   ```

## Acknowledgments

This project started as a fork of [aws_wiz](https://github.com/besarthoxhaj/aws_wiz) by [Bes](https://github.com/besarthoxhaj) and has been inspired by discussions with colleagues from the [Encode: AI for Science Fellowship](https://encode.pillar.vc/).