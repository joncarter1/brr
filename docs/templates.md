# Cluster Templates

brr cluster templates are [Ray cluster YAML files](https://docs.ray.io/en/latest/cluster/vms/references/ray-cluster-configuration.html) with extensions for config placeholders, automatic setup injection, and CLI overrides.

## Built-in templates

| Name | Provider | Head node | Workers | GPU |
|------|----------|-----------|---------|-----|
| `cpu` | AWS | t3.2xlarge | t3.large (max 2) | - |
| `l4` | AWS | gr6.4xlarge | - | 1x L4 |
| `h100` | AWS | p5.4xlarge | - | 8x H100 |
| `cpu-l4` | AWS | t3.2xlarge | g6.4xlarge (max 4) | 1x L4 |
| `cpu` | Nebius | 8vcpu-32gb | 4vcpu-16gb (max 2) | - |
| `h100` | Nebius | 1gpu-16vcpu-200gb | - | 1x H100 |
| `cpu-h100s` | Nebius | 8vcpu-32gb | 8gpu-128vcpu-1600gb (max 4) | 8x H100 |

Use them directly (`brr up aws:l4`) or as the basis for project templates (`brr init`).

## Template resolution

brr looks for templates in this order:

1. **Direct path** — if the name contains `/` or ends with `.yaml` (e.g. `brr up ./custom.yaml`)
2. **Project template** — `.brr/{provider}/{name}.yaml` when inside a project (has `.brr/` dir)
3. **Built-in template** — `brr/{provider}/templates/{name}.yaml`

Use an explicit provider prefix to bypass the project and use a built-in: `brr up aws:l4`.

## Placeholders

Templates use `{{VAR}}` placeholders that are filled from `~/.brr/config.env` (can be set via `brr configure`):

### AWS
| Placeholder | Description |
|------------|-------------|
| `{{AWS_SSH_KEY}}` | Path to SSH private key |
| `{{AWS_KEY_NAME}}` | EC2 key pair name |
| `{{AWS_SECURITY_GROUP}}` | Security group ID |
| `{{AMI_UBUNTU}}` | Ubuntu AMI (CPU nodes) |
| `{{AMI_DL}}` | Deep Learning AMI (GPU nodes) |

### Nebius
| Placeholder | Description |
|------------|-------------|
| `{{NEBIUS_SSH_KEY}}` | Path to SSH private key |
| `{{NEBIUS_PROJECT_ID}}` | Nebius project ID |
| `{{NEBIUS_SUBNET_ID}}` | Subnet ID |
| `{{NEBIUS_FILESYSTEM_ID}}` | Shared filesystem ID |

## What brr injects

When you run `brr up`, the template is processed through several steps before being passed to `ray up`. The function `inject_brr_infra()` adds:

- **`file_mounts`** — mounts a staging directory to `/tmp/brr/` on all nodes, containing setup scripts, config, and credentials. Any `file_mounts` you define in the template are preserved — brr merges its entry alongside yours.
- **`initialization_commands`** — installs pip (required by Ray before setup runs)
- **`setup_commands`** — prepends `bash /tmp/brr/setup.sh` (global setup) and `bash /tmp/brr/project-setup.sh` (project setup, if in a project). Any `setup_commands` you add to the template run after these.
- **`head_setup_commands`** — appends `bash /tmp/brr/sync-repo.sh` to clone the project repo on first deploy (only if inside a git repo with a remote)

Use `brr up <template> --dry-run` to see the final YAML after all injection and rendering.

### Setup layering

On every node boot, scripts run in this order:

1. **Global setup** (built-in) — packages, filesystem mounts, uv, Python venv, Ray, AI tools, dotfiles, idle shutdown
2. **Project setup** (`.brr/setup.sh`) — project-specific dependencies
3. **User setup_commands** — anything you add to `setup_commands:` in the template
4. **Sync repo** (head node only, first deploy) — clones the git repo to `~/code/{repo_name}`

## CLI overrides

Override any field from the command line:

```
brr up aws:dev instance_type=g6.2xlarge max_workers=8 spot=true
```

### Global override names

These are shorthand names that work with any template:

| Name | Maps to | Description |
|------|---------|-------------|
| `instance_type` | `available_node_types.ray.head.default.node_config.InstanceType` | Head node instance type |
| `max_workers` | `max_workers` | Maximum worker count |
| `region` | `provider.region` | AWS region |
| `az` | `provider.availability_zone` | Availability zone |
| `ami` | `available_node_types.ray.head.default.node_config.ImageId` | Head node AMI |
| `spot` | (special) | Enable/disable spot pricing |
| `capacity_reservation` | (special) | AWS capacity reservation ID e.g. for H100s |

### Dot-notation

For fields without a shorthand, use dot-notation paths:

```
brr up aws:dev available_node_types.ray.worker.default.node_config.InstanceType=g6.12xlarge
```

### Template aliases (`_brr` section)

Define custom shorthand names in your template:

```yaml
_brr:
  worker_type: available_node_types.ray.worker.default.node_config.InstanceType
  disk: available_node_types.ray.head.default.node_config.BlockDeviceMappings.0.Ebs.VolumeSize

cluster_name: my-cluster
# ... rest of template
```

Then use them: `brr up aws:dev worker_type=g6.12xlarge disk=500`

The `_brr` section is stripped from the final YAML before passing to Ray.

## Nebius node_config

Nebius uses a custom Ray node provider, so `node_config` fields differ from AWS:

| Field | Description | Examples |
|-------|-------------|----------|
| `platform_id` | Compute platform | `cpu-e2`, `gpu-h100-sxm` |
| `preset_id` | vCPU/RAM/GPU preset | `8vcpu-32gb`, `1gpu-16vcpu-200gb`, `8gpu-128vcpu-1600gb` |
| `image_family` | OS image | `ubuntu22.04-driverless` (CPU), `ubuntu22.04-cuda12` (GPU) |
| `subnet_id` | Network subnet | Set by `brr configure` |
| `disk_size_gb` | Boot disk size in GB | `50` |
| `disk_type` | Disk type | `network-ssd` |

**Important**: Nebius nodes must declare `resources:` explicitly (e.g. `CPU: 8`, `GPU: 1`) because Ray cannot auto-detect resources on external providers.

### Nebius autoscaling

- `cache_stopped_nodes: true` — stop nodes on scale-down instead of deleting (faster restart, but incurs disk fees while stopped)
- `cache_stopped_nodes: false` — delete nodes on scale-down (no idle costs, slower to scale back up)
