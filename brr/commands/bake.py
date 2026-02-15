"""brr bake — pre-bake global setup into cloud images for fast spin-up."""

import asyncio
import hashlib
import signal
import subprocess
import time

import click
from rich.console import Console

from brr.state import read_config, write_config, STATE_DIR
from brr.templates import _read_global_setup

console = Console()

# Base images to bake and their config keys
_IMAGE_TYPES = {
    "cpu": {"base_key": "AMI_UBUNTU", "baked_key": "AMI_UBUNTU_BAKED", "label": "CPU (Ubuntu)"},
    "gpu": {"base_key": "AMI_DL", "baked_key": "AMI_DL_BAKED", "label": "GPU (Deep Learning)"},
}

_NEBIUS_IMAGE_TYPES = {
    "cpu": {
        "image_family": "ubuntu22.04-driverless",
        "baked_key": "NEBIUS_IMAGE_CPU_BAKED",
        "label": "CPU (Ubuntu driverless)",
    },
    "gpu": {
        "image_family": "ubuntu22.04-cuda12",
        "baked_key": "NEBIUS_IMAGE_GPU_BAKED",
        "label": "GPU (CUDA 12)",
    },
}


def _setup_hash():
    """MD5 of the global setup.sh content."""
    return hashlib.md5(_read_global_setup().encode()).hexdigest()


def _wait_ssh(ip, key_path, timeout=180):
    """Poll until SSH is reachable."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            [
                "ssh", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                "-i", key_path, f"ubuntu@{ip}", "true",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        time.sleep(5)
    return False


def _upload_staging(ip, key_path, staging_dir):
    """Upload staging files to ~/brr/ on the remote instance."""
    result = subprocess.run(
        [
            "scp", "-o", "StrictHostKeyChecking=accept-new",
            "-i", key_path, "-r",
        ] + [str(f) for f in staging_dir.iterdir()] + [f"ubuntu@{ip}:brr/"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"scp failed: {result.stderr.strip()}")


def _run_remote(ip, key_path, command):
    """Run a command on the remote instance, streaming output."""
    result = subprocess.run(
        [
            "ssh", "-o", "StrictHostKeyChecking=accept-new",
            "-i", key_path, f"ubuntu@{ip}",
            command,
        ],
    )
    return result.returncode


# Keys to strip from config.env during baking — secrets shouldn't be baked
# into the AMI, and mounts/dotfiles require credentials not available on
# the ephemeral bake instance.
_BAKE_STRIP_KEYS = {
    "GITHUB_SSH_SECRET", "EC2_SSH_SECRET",
    "GITHUB_SSH_KEY",
    "EFS_ID", "NEBIUS_FILESYSTEM_ID",
    "DOTFILES_REPO",
}


def _prepare_bake_staging(provider="aws"):
    """Prepare a minimal staging directory for baking (no project setup).

    Strips secrets and mount-related keys from config.env so setup.sh skips
    those sections. They'll run on actual cluster boot when config.env has them.
    """
    import re
    from importlib.resources import files

    staging = STATE_DIR / "bake-staging"
    staging.mkdir(parents=True, exist_ok=True)

    # Global setup.sh
    (staging / "setup.sh").write_text(_read_global_setup())

    # idle-shutdown.sh
    pkg = files("brr.data")
    (staging / "idle-shutdown.sh").write_text(pkg.joinpath("idle-shutdown.sh").read_text())

    # config.env — strip secrets and mount-related keys
    from brr.state import CONFIG_PATH
    config_text = ""
    if CONFIG_PATH.exists():
        lines = []
        for line in CONFIG_PATH.read_text().splitlines():
            m = re.match(r'^([A-Z0-9_]+)="', line)
            if m and m.group(1) in _BAKE_STRIP_KEYS:
                continue
            lines.append(line)
        config_text = "\n".join(lines) + "\n"
    if config_text.strip() and 'PROVIDER="' not in config_text:
        config_text = config_text.rstrip("\n") + f'\nPROVIDER="{provider}"\n'
    if config_text.strip():
        (staging / "config.env").write_text(config_text)

    return staging


def _bake_aws_image(image_type, config):
    """Bake a single AMI from the global setup. Returns the new AMI ID."""
    info = _IMAGE_TYPES[image_type]
    base_ami = config.get(info["base_key"], "")
    if not base_ami:
        console.print(f"[red]{info['base_key']} not configured. Run `brr configure aws` first.[/red]")
        return None

    region = config.get("AWS_REGION", "us-east-1")
    key_name = config.get("AWS_KEY_NAME", "")
    key_path = config.get("AWS_SSH_KEY", "")
    sg_id = config.get("AWS_SECURITY_GROUP", "")

    if not all([key_name, key_path, sg_id]):
        console.print("[red]AWS not fully configured. Run `brr configure aws` first.[/red]")
        return None

    import boto3
    ec2 = boto3.client("ec2", region_name=region)
    ec2_resource = boto3.resource("ec2", region_name=region)

    console.print(f"\n[bold]{info['label']}[/bold]: baking from {base_ami}")

    # Launch temp instance
    with console.status("[bold green]Launching temp instance..."):
        instances = ec2_resource.create_instances(
            ImageId=base_ami,
            InstanceType="t3.medium",
            KeyName=key_name,
            SecurityGroupIds=[sg_id],
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"brr-bake-{image_type}"},
                    {"Key": "brr-bake", "Value": "true"},
                ],
            }],
        )
        instance = instances[0]
        instance_id = instance.id
        console.print(f"Instance: [green]{instance_id}[/green]")

    try:  # cleanup starts here — ctrl+c after creation always terminates
        # Wait for running
        with console.status("[bold green]Waiting for instance to start..."):
            instance.wait_until_running()
            instance.reload()
            ip = instance.public_ip_address

        if not ip:
            console.print("[red]Instance has no public IP. Check your VPC/subnet configuration.[/red]")
            return None

        console.print(f"IP: [green]{ip}[/green]")

        # Wait for SSH
        with console.status("[bold green]Waiting for SSH..."):
            if not _wait_ssh(ip, key_path):
                console.print("[red]SSH not reachable after 3 minutes.[/red]")
                return None

        console.print("[green]SSH connected[/green]")

        # Prepare and upload staging
        staging = _prepare_bake_staging()
        with console.status("[bold green]Uploading setup files..."):
            # Ensure ~/brr/ exists on remote
            _run_remote(ip, key_path, "mkdir -p ~/brr")
            _upload_staging(ip, key_path, staging)

        console.print("[green]Files uploaded[/green]")

        # Run setup.sh
        console.print("[bold]Running setup.sh...[/bold]")
        rc = _run_remote(ip, key_path, "bash ~/brr/setup.sh")
        if rc != 0:
            console.print(f"[red]setup.sh failed with exit code {rc}[/red]")
            return None

        console.print("[green]Setup complete[/green]")

        # Clean up staging files on remote so they don't end up in the AMI
        _run_remote(ip, key_path, "rm -rf ~/brr")

        # Create AMI
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        ami_name = f"brr-{image_type}-{timestamp}"
        with console.status(f"[bold green]Creating AMI '{ami_name}'..."):
            response = ec2.create_image(
                InstanceId=instance_id,
                Name=ami_name,
                Description=f"brr baked {info['label']} image",
                NoReboot=False,
            )
            new_ami = response["ImageId"]

            # Tag the AMI
            ec2.create_tags(
                Resources=[new_ami],
                Tags=[
                    {"Key": "Name", "Value": ami_name},
                    {"Key": "brr-baked", "Value": "true"},
                    {"Key": "brr-base-ami", "Value": base_ami},
                ],
            )

            # Wait for AMI to be available
            waiter = ec2.get_waiter("image_available")
            waiter.wait(ImageIds=[new_ami], WaiterConfig={"Delay": 15, "MaxAttempts": 80})

        console.print(f"AMI: [bold green]{new_ami}[/bold green] ({ami_name})")
        return new_ami

    except KeyboardInterrupt:
        console.print(f"\n[yellow]Interrupted — cleaning up {instance_id}...[/yellow]")
        return None
    finally:
        # Terminate temp instance — ignore further ctrl+c during cleanup
        prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            with console.status("[bold green]Terminating temp instance..."):
                ec2.terminate_instances(InstanceIds=[instance_id])
            console.print(f"Terminated {instance_id}")
        finally:
            signal.signal(signal.SIGINT, prev)


@click.group()
def bake():
    """Pre-bake global setup into cloud images for fast spin-up."""


@bake.command()
@click.option("--type", "image_type", type=click.Choice(["cpu", "gpu", "all"]),
              default="all", help="Which base images to bake (default: all)")
def aws(image_type):
    """Bake global setup into AWS AMIs.

    Creates AMIs with packages, venv, Ray, Claude Code, etc. pre-installed.
    Cluster boot time drops from ~5 min to ~15 sec.
    """
    config = read_config()
    if not config.get("AWS_REGION"):
        console.print("[red]AWS not configured. Run `brr configure aws` first.[/red]")
        raise SystemExit(1)

    types_to_bake = list(_IMAGE_TYPES.keys()) if image_type == "all" else [image_type]

    results = {}
    for it in types_to_bake:
        new_ami = _bake_aws_image(it, config)
        if new_ami:
            results[it] = new_ami

    if not results:
        console.print("[red]No images were baked.[/red]")
        raise SystemExit(1)

    # Deregister old baked AMIs
    import boto3
    region = config.get("AWS_REGION", "us-east-1")
    ec2 = boto3.client("ec2", region_name=region)
    for it, new_ami in results.items():
        old_ami = config.get(_IMAGE_TYPES[it]["baked_key"], "")
        if old_ami and old_ami != new_ami:
            try:
                ec2.deregister_image(ImageId=old_ami)
                console.print(f"Deregistered old AMI: [dim]{old_ami}[/dim]")
            except Exception:
                pass  # Old AMI may already be gone

    # Update config with new AMIs and setup hash
    for it, new_ami in results.items():
        config[_IMAGE_TYPES[it]["baked_key"]] = new_ami
    config["BAKE_SETUP_HASH"] = _setup_hash()
    write_config(config)
    console.print(f"\nWrote baked AMI IDs to [green]{STATE_DIR / 'config.env'}[/green]")

    console.print()
    console.print("[bold green]Done![/bold green] Baked images will be used automatically on next `brr up`.")


# ---------------------------------------------------------------------------
# Nebius
# ---------------------------------------------------------------------------

async def _bake_nebius_image_async(image_type, config):
    """Bake a single Nebius image from the global setup. Returns the new image ID."""
    from nebius.api.nebius.common.v1 import ResourceMetadata
    from nebius.api.nebius.compute.v1 import (
        CreateDiskRequest,
        CreateInstanceRequest,
        DeleteInstanceRequest,
        DiskServiceClient,
        DiskSpec,
        GetInstanceRequest,
        ImageServiceClient,
        CreateImageRequest,
        ImageSpec,
        InstanceServiceClient,
        InstanceSpec,
        InstanceRecoveryPolicy,
        ResourcesSpec,
        AttachedDiskSpec,
        ExistingDisk,
        NetworkInterfaceSpec,
        IPAddress,
        PublicIPAddress,
        SourceImageFamily,
        StopInstanceRequest,
    )
    from brr.nebius.nodes import _nebius_sdk

    info = _NEBIUS_IMAGE_TYPES[image_type]
    project_id = config["NEBIUS_PROJECT_ID"]
    subnet_id = config["NEBIUS_SUBNET_ID"]
    ssh_key_path = config["NEBIUS_SSH_KEY"]

    console.print(f"\n[bold]{info['label']}[/bold]: baking from {info['image_family']}")

    sdk = _nebius_sdk()
    async with sdk:
        disk_client = DiskServiceClient(sdk)
        instance_client = InstanceServiceClient(sdk)
        image_client = ImageServiceClient(sdk)

        uid = __import__("uuid").uuid4().hex[:8]
        name = f"brr-bake-{image_type}-{uid}"

        # 1. Create boot disk from base image family
        with console.status("[bold green]Creating boot disk..."):
            disk_op = await disk_client.create(CreateDiskRequest(
                metadata=ResourceMetadata(
                    parent_id=project_id,
                    name=f"{name}-boot",
                ),
                spec=DiskSpec(
                    type=DiskSpec.DiskType.NETWORK_SSD,
                    source_image_family=SourceImageFamily(
                        image_family=info["image_family"],
                    ),
                    size_gibibytes=100,
                ),
            ))
            await disk_op.wait()
            disk_id = disk_op.resource_id
            console.print(f"Disk: [green]{disk_id}[/green]")

        # Read SSH public key for cloud-init
        pub_key_path = ssh_key_path + ".pub"
        try:
            with open(pub_key_path) as f:
                pubkey = f.read().strip()
            cloud_init = f"#cloud-config\nssh_authorized_keys:\n  - {pubkey}\n"
        except FileNotFoundError:
            console.print(f"[red]SSH public key not found: {pub_key_path}[/red]")
            return None

        # 2. Create ephemeral instance (cheap CPU platform for both image types)
        with console.status("[bold green]Launching temp instance..."):
            inst_op = await instance_client.create(CreateInstanceRequest(
                metadata=ResourceMetadata(
                    parent_id=project_id,
                    name=name,
                    labels={"brr-bake": "true"},
                ),
                spec=InstanceSpec(
                    recovery_policy=InstanceRecoveryPolicy.FAIL,
                    resources=ResourcesSpec(
                        platform="cpu-e2",
                        preset="8vcpu-32gb",
                    ),
                    boot_disk=AttachedDiskSpec(
                        attach_mode=AttachedDiskSpec.AttachMode.READ_WRITE,
                        existing_disk=ExistingDisk(id=disk_id),
                    ),
                    network_interfaces=[
                        NetworkInterfaceSpec(
                            name="eth0",
                            subnet_id=subnet_id,
                            ip_address=IPAddress(),
                            public_ip_address=PublicIPAddress(),
                        ),
                    ],
                    cloud_init_user_data=cloud_init,
                ),
            ))
            await inst_op.wait()
            instance_id = inst_op.resource_id
            console.print(f"Instance: [green]{instance_id}[/green]")

        try:
            # 3. Wait for RUNNING state
            with console.status("[bold green]Waiting for instance to start..."):
                deadline = time.time() + 180
                ip = None
                while time.time() < deadline:
                    inst = await instance_client.get(GetInstanceRequest(id=instance_id))
                    state = inst.status.state if inst.status else None
                    if "RUNNING" in str(state) or state == 4:
                        ip = None
                        ifaces = inst.status.network_interfaces if inst.status else []
                        if ifaces:
                            addr = getattr(ifaces[0], "public_ip_address", None)
                            if addr and addr.address:
                                ip = addr.address.split("/")[0]
                        break
                    await asyncio.sleep(5)

            if not ip:
                console.print("[red]Instance has no public IP or failed to start.[/red]")
                return None

            console.print(f"IP: [green]{ip}[/green]")

            # 4. Wait for SSH
            with console.status("[bold green]Waiting for SSH..."):
                if not _wait_ssh(ip, ssh_key_path):
                    console.print("[red]SSH not reachable after 3 minutes.[/red]")
                    return None

            console.print("[green]SSH connected[/green]")

            # 5. Prepare and upload staging
            staging = _prepare_bake_staging(provider="nebius")
            with console.status("[bold green]Uploading setup files..."):
                _run_remote(ip, ssh_key_path, "mkdir -p ~/brr")
                _upload_staging(ip, ssh_key_path, staging)

            console.print("[green]Files uploaded[/green]")

            # 6. Run setup.sh
            console.print("[bold]Running setup.sh...[/bold]")
            rc = _run_remote(ip, ssh_key_path, "bash ~/brr/setup.sh")
            if rc != 0:
                console.print(f"[red]setup.sh failed with exit code {rc}[/red]")
                return None

            console.print("[green]Setup complete[/green]")

            # 7. Clean up staging on remote
            _run_remote(ip, ssh_key_path, "rm -rf ~/brr")

            # 8. Stop instance (image creation requires disk not attached to running instance)
            with console.status("[bold green]Stopping instance..."):
                stop_op = await instance_client.stop(StopInstanceRequest(id=instance_id))
                await stop_op.wait()

            console.print("[green]Instance stopped[/green]")

            # 9. Create image from boot disk
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            image_name = f"brr-{image_type}-{timestamp}"
            with console.status(f"[bold green]Creating image '{image_name}'..."):
                try:
                    img_op = await image_client.create(CreateImageRequest(
                        metadata=ResourceMetadata(
                            parent_id=project_id,
                            name=image_name,
                            labels={"brr-baked": "true", "brr-image-type": image_type},
                        ),
                        spec=ImageSpec(source_disk_id=disk_id),
                    ))
                    await img_op.wait()
                    new_image_id = img_op.resource_id
                except Exception as e:
                    if "RESOURCE_EXHAUSTED" in str(e) or "quota" in str(e).lower():
                        console.print(
                            "\n[red]Quota exceeded for custom images.[/red]\n"
                            "Increase these quotas in your Nebius tenant settings:\n"
                            "  • compute.image.count\n"
                            "  • compute.image.size\n"
                        )
                        return None
                    raise

            console.print(f"Image: [bold green]{new_image_id}[/bold green] ({image_name})")
            return new_image_id

        except KeyboardInterrupt:
            console.print(f"\n[yellow]Interrupted — cleaning up...[/yellow]")
            return None
        finally:
            prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                with console.status("[bold green]Deleting temp instance..."):
                    try:
                        del_op = await instance_client.delete(
                            DeleteInstanceRequest(id=instance_id)
                        )
                        await del_op.wait()
                    except Exception:
                        pass
                console.print(f"Deleted {instance_id}")
            finally:
                signal.signal(signal.SIGINT, prev)


def _bake_nebius_image(image_type, config):
    """Sync wrapper for the async Nebius bake flow."""
    return asyncio.run(_bake_nebius_image_async(image_type, config))


def _delete_old_nebius_images(config, results):
    """Delete previously baked Nebius images that are being replaced."""
    old_ids = []
    for it, new_id in results.items():
        old_id = config.get(_NEBIUS_IMAGE_TYPES[it]["baked_key"], "")
        if old_id and old_id != new_id:
            old_ids.append(old_id)

    if not old_ids:
        return

    async def _delete():
        from nebius.api.nebius.compute.v1 import ImageServiceClient, DeleteImageRequest
        from brr.nebius.nodes import _nebius_sdk

        sdk = _nebius_sdk()
        async with sdk:
            client = ImageServiceClient(sdk)
            for img_id in old_ids:
                try:
                    op = await client.delete(DeleteImageRequest(id=img_id))
                    await op.wait()
                    console.print(f"Deleted old image: [dim]{img_id}[/dim]")
                except Exception:
                    pass

    asyncio.run(_delete())


@bake.command()
@click.option("--type", "image_type", type=click.Choice(["cpu", "gpu", "all"]),
              default="all", help="Which base images to bake (default: all)")
def nebius(image_type):
    """Bake global setup into Nebius images.

    Creates images with packages, venv, Ray, Claude Code, etc. pre-installed.
    Cluster boot time drops from ~5 min to ~15 sec.
    """
    config = read_config()
    if not config.get("NEBIUS_PROJECT_ID"):
        console.print("[red]Nebius not configured. Run `brr configure nebius` first.[/red]")
        raise SystemExit(1)

    for key in ("NEBIUS_SUBNET_ID", "NEBIUS_SSH_KEY"):
        if not config.get(key):
            console.print(f"[red]{key} not configured. Run `brr configure nebius` first.[/red]")
            raise SystemExit(1)

    types_to_bake = list(_NEBIUS_IMAGE_TYPES.keys()) if image_type == "all" else [image_type]

    results = {}
    for it in types_to_bake:
        new_image = _bake_nebius_image(it, config)
        if new_image:
            results[it] = new_image

    if not results:
        console.print("[red]No images were baked.[/red]")
        raise SystemExit(1)

    # Delete old baked images
    _delete_old_nebius_images(config, results)

    # Update config with new image IDs and setup hash
    for it, new_id in results.items():
        config[_NEBIUS_IMAGE_TYPES[it]["baked_key"]] = new_id
    config["NEBIUS_BAKE_SETUP_HASH"] = _setup_hash()
    write_config(config)
    console.print(f"\nWrote baked image IDs to [green]{STATE_DIR / 'config.env'}[/green]")

    console.print()
    console.print("[bold green]Done![/bold green] Baked images will be used automatically on next `brr up`.")


@bake.command()
def status():
    """Show current baked image info."""
    config = read_config()

    has_baked = False
    current_hash = _setup_hash()

    # AWS
    for info in _IMAGE_TYPES.values():
        baked = config.get(info["baked_key"], "")
        base = config.get(info["base_key"], "")
        if baked:
            has_baked = True
            console.print(f"[bold]AWS — {info['label']}[/bold]")
            console.print(f"  Base:  {base}")
            console.print(f"  Baked: [green]{baked}[/green]")

    # Nebius
    for info in _NEBIUS_IMAGE_TYPES.values():
        baked = config.get(info["baked_key"], "")
        if baked:
            has_baked = True
            console.print(f"[bold]Nebius — {info['label']}[/bold]")
            console.print(f"  Family: {info['image_family']}")
            console.print(f"  Baked:  [green]{baked}[/green]")

    if not has_baked:
        console.print("[dim]No baked images. Run `brr bake aws` or `brr bake nebius` to create them.[/dim]")
        return

    # Check staleness
    aws_hash = config.get("BAKE_SETUP_HASH", "")
    if aws_hash:
        if aws_hash == current_hash:
            console.print(f"\n[green]AWS setup hash matches — baked images are up to date.[/green]")
        else:
            console.print(f"\n[yellow]AWS setup hash mismatch — run `brr bake aws` to rebuild.[/yellow]")

    nebius_hash = config.get("NEBIUS_BAKE_SETUP_HASH", "")
    if nebius_hash:
        if nebius_hash == current_hash:
            console.print(f"[green]Nebius setup hash matches — baked images are up to date.[/green]")
        else:
            console.print(f"[yellow]Nebius setup hash mismatch — run `brr bake nebius` to rebuild.[/yellow]")
