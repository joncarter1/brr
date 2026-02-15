import os
import shutil
import subprocess

import click
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel

from brr.utils import get_regions

console = Console()

def terminate_instances(region):
    """Terminate all EC2 instances in a region"""
    import boto3
    terminated = []
    try:
        ec2 = boto3.client('ec2', region_name=region)
        response = ec2.describe_instances()

        for reservation in response['Reservations']:
            for instance in reservation['Instances']:
                if instance['State']['Name'] not in ['terminated', 'terminating']:
                    instance_id = instance['InstanceId']
                    name = 'Unknown'
                    for tag in instance.get('Tags', []):
                        if tag['Key'] == 'Name':
                            name = tag['Value']
                            break

                    try:
                        ec2.terminate_instances(InstanceIds=[instance_id])
                        terminated.append((instance_id, name, region))
                    except Exception as e:
                        console.print(f"[red]Error terminating {instance_id}: {e}[/red]")
    except Exception as e:
        if 'AuthFailure' not in str(e):
            console.print(f"[red]Error in {region}: {e}[/red]")

    return terminated

def delete_vpcs(region):
    """Delete all non-default VPCs and their dependencies"""
    import boto3
    deleted_vpcs = []
    try:
        ec2 = boto3.client('ec2', region_name=region)

        # Get all VPCs
        vpcs = ec2.describe_vpcs(Filters=[{'Name': 'is-default', 'Values': ['false']}])

        for vpc in vpcs['Vpcs']:
            vpc_id = vpc['VpcId']
            vpc_name = vpc_id
            for tag in vpc.get('Tags', []):
                if tag['Key'] == 'Name':
                    vpc_name = tag['Value']
                    break

            try:
                # Delete subnets
                subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
                for subnet in subnets['Subnets']:
                    try:
                        ec2.delete_subnet(SubnetId=subnet['SubnetId'])
                    except Exception:
                        pass

                # Delete route tables
                route_tables = ec2.describe_route_tables(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
                for rt in route_tables['RouteTables']:
                    if not any(assoc.get('Main', False) for assoc in rt.get('Associations', [])):
                        try:
                            ec2.delete_route_table(RouteTableId=rt['RouteTableId'])
                        except Exception:
                            pass

                # Detach and delete internet gateways
                igws = ec2.describe_internet_gateways(Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}])
                for igw in igws['InternetGateways']:
                    try:
                        ec2.detach_internet_gateway(InternetGatewayId=igw['InternetGatewayId'], VpcId=vpc_id)
                        ec2.delete_internet_gateway(InternetGatewayId=igw['InternetGatewayId'])
                    except Exception:
                        pass

                # Delete NAT gateways
                nat_gateways = ec2.describe_nat_gateways(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
                for nat in nat_gateways['NatGateways']:
                    if nat['State'] not in ['deleted', 'deleting']:
                        try:
                            ec2.delete_nat_gateway(NatGatewayId=nat['NatGatewayId'])
                        except Exception:
                            pass

                # Delete security groups
                sgs = ec2.describe_security_groups(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
                for sg in sgs['SecurityGroups']:
                    if sg['GroupName'] != 'default':
                        try:
                            ec2.delete_security_group(GroupId=sg['GroupId'])
                        except Exception:
                            pass

                # Delete VPC
                ec2.delete_vpc(VpcId=vpc_id)
                deleted_vpcs.append((vpc_id, vpc_name, region))

            except Exception as e:
                console.print(f"[red]Error deleting VPC {vpc_id}: {e}[/red]")

    except Exception as e:
        if 'AuthFailure' not in str(e):
            console.print(f"[red]Error in {region}: {e}[/red]")

    return deleted_vpcs

def release_elastic_ips(region):
    """Release all Elastic IPs"""
    import boto3
    released = []
    try:
        ec2 = boto3.client('ec2', region_name=region)
        eips = ec2.describe_addresses()

        for eip in eips['Addresses']:
            try:
                if 'AssociationId' in eip:
                    ec2.disassociate_address(AssociationId=eip['AssociationId'])
                ec2.release_address(AllocationId=eip['AllocationId'])
                released.append((eip.get('PublicIp', 'Unknown'), region))
            except Exception as e:
                console.print(f"[red]Error releasing EIP: {e}[/red]")

    except Exception:
        pass

    return released

def delete_key_pairs(region):
    """Delete all key pairs"""
    import boto3
    deleted = []
    try:
        ec2 = boto3.client('ec2', region_name=region)
        key_pairs = ec2.describe_key_pairs()

        for kp in key_pairs['KeyPairs']:
            try:
                ec2.delete_key_pair(KeyName=kp['KeyName'])
                deleted.append((kp['KeyName'], region))
            except Exception as e:
                console.print(f"[red]Error deleting key pair {kp['KeyName']}: {e}[/red]")

    except Exception:
        pass

    return deleted

def delete_volumes(region):
    """Delete all available EBS volumes"""
    import boto3
    deleted = []
    try:
        ec2 = boto3.client('ec2', region_name=region)
        volumes = ec2.describe_volumes(Filters=[{'Name': 'status', 'Values': ['available']}])

        for volume in volumes['Volumes']:
            try:
                ec2.delete_volume(VolumeId=volume['VolumeId'])
                deleted.append((volume['VolumeId'], volume['Size'], region))
            except Exception as e:
                console.print(f"[red]Error deleting volume {volume['VolumeId']}: {e}[/red]")

    except Exception:
        pass

    return deleted

def delete_github_ssh(region):
    """Remove GitHub SSH key from Secrets Manager and GitHub."""
    import boto3
    secret_name = "brr-github-ssh-key"
    deleted = []

    # Secrets Manager — try delete, handle missing
    sm = boto3.client("secretsmanager", region_name=region)
    try:
        sm.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
        deleted.append(secret_name)
        console.print(f"  Deleted Secrets Manager secret: [red]{secret_name}[/red]")
    except sm.exceptions.ResourceNotFoundException:
        console.print(f"  No Secrets Manager secret found ({secret_name})")

    # GitHub — find key by title, delete by ID
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "ssh-key", "list"], capture_output=True, text=True
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "brr-cluster" in line:
                    key_id = line.split()[0]
                    del_result = subprocess.run(
                        ["gh", "ssh-key", "delete", key_id, "--yes"],
                        capture_output=True, text=True,
                    )
                    if del_result.returncode == 0:
                        deleted.append(f"github:{key_id}")
                        console.print(f"  Deleted GitHub SSH key: [red]{key_id}[/red]")
                    else:
                        console.print(f"  [yellow]Failed to delete GitHub key {key_id}: {del_result.stderr.strip()}[/yellow]")
                    break
            else:
                console.print("  No GitHub SSH key found with title 'brr-cluster'")
    else:
        console.print("  [yellow]gh CLI not found — skip GitHub key cleanup[/yellow]")

    # Remove IAM inline policies from ray-autoscaler role
    iam = boto3.client("iam")
    for policy_name in ["brr-secretsmanager-read", "brr-iam-passrole"]:
        try:
            iam.delete_role_policy(
                RoleName="ray-autoscaler-v1",
                PolicyName=policy_name,
            )
            deleted.append(f"iam:{policy_name}")
            console.print(f"  Deleted IAM inline policy: [red]{policy_name}[/red]")
        except iam.exceptions.NoSuchEntityException:
            console.print(f"  No IAM inline policy found ({policy_name})")

    # Detach SSM managed policy
    try:
        iam.detach_role_policy(
            RoleName="ray-autoscaler-v1",
            PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        )
        console.print(f"  Detached SSM managed policy from [red]ray-autoscaler-v1[/red]")
    except iam.exceptions.NoSuchEntityException:
        pass

    return deleted


def _nuke_nebius(project_id, progress, task):
    """Terminate all Nebius instances and delete disks in a project."""
    import asyncio
    from brr.nebius.nodes import _nebius_sdk

    async def _nuke():
        from nebius.api.nebius.compute.v1 import (
            InstanceServiceClient, ListInstancesRequest, DeleteInstanceRequest,
            DiskServiceClient, ListDisksRequest, DeleteDiskRequest,
            FilesystemServiceClient, ListFilesystemsRequest, DeleteFilesystemRequest,
        )

        sdk = _nebius_sdk()
        stats = {"instances": 0, "disks": 0, "filesystems": 0}

        async with sdk:
            # Phase 1: Terminate all instances
            progress.update(task, description="[red]Terminating Nebius instances...[/red]")
            inst_client = InstanceServiceClient(sdk)
            resp = await inst_client.list(ListInstancesRequest(parent_id=project_id))

            for inst in resp.items:
                state = inst.status.state if inst.status else None
                # Skip already terminated/deleting
                state_str = str(state)
                if any(s in state_str for s in ("DELETED", "DELETING", "7", "8")):
                    continue
                try:
                    name = inst.metadata.name or inst.metadata.id
                    op = await inst_client.delete(DeleteInstanceRequest(id=inst.metadata.id))
                    await op.wait()
                    stats["instances"] += 1
                    console.print(f"  Terminated: [red]{name}[/red]")
                except Exception as e:
                    console.print(f"  [yellow]Failed to terminate {inst.metadata.id}: {e}[/yellow]")

            # Phase 2: Delete disks
            progress.update(task, description="[red]Deleting Nebius disks...[/red]")
            disk_client = DiskServiceClient(sdk)
            resp = await disk_client.list(ListDisksRequest(parent_id=project_id))

            for disk in resp.items:
                try:
                    name = disk.metadata.name or disk.metadata.id
                    op = await disk_client.delete(DeleteDiskRequest(id=disk.metadata.id))
                    await op.wait()
                    stats["disks"] += 1
                    console.print(f"  Deleted disk: [red]{name}[/red]")
                except Exception as e:
                    console.print(f"  [yellow]Failed to delete disk {disk.metadata.id}: {e}[/yellow]")

            # Phase 3: Delete filesystems
            progress.update(task, description="[red]Deleting Nebius filesystems...[/red]")
            fs_client = FilesystemServiceClient(sdk)
            resp = await fs_client.list(ListFilesystemsRequest(parent_id=project_id))

            for fs in resp.items:
                try:
                    name = fs.metadata.name or fs.metadata.id
                    op = await fs_client.delete(DeleteFilesystemRequest(id=fs.metadata.id))
                    await op.wait()
                    stats["filesystems"] += 1
                    console.print(f"  Deleted filesystem: [red]{name}[/red]")
                except Exception as e:
                    console.print(f"  [yellow]Failed to delete filesystem {fs.metadata.id}: {e}[/yellow]")

        return stats

    return asyncio.run(_nuke())


@click.command()
@click.option('--force', is_flag=True, help='Skip confirmation prompt')
@click.option('--region', help='Specific AWS region to nuke (default: all regions)')
@click.option('--provider', type=click.Choice(['aws', 'nebius', 'all']), default='all',
              help='Provider to nuke (default: all configured)')
def nuke(force, region, provider):
    """Nuclear option: Delete ALL cloud resources."""
    from brr.state import read_config

    config = read_config() or {}
    has_aws = bool(config.get("AWS_REGION"))
    has_nebius = bool(config.get("NEBIUS_PROJECT_ID"))

    targets = []
    if provider in ("aws", "all") and has_aws:
        targets.append("aws")
    if provider in ("nebius", "all") and has_nebius:
        targets.append("nebius")

    if not targets:
        console.print("[yellow]No configured providers to nuke.[/yellow]")
        return

    # Warning panel
    items = []
    if "aws" in targets:
        items.extend([
            "- All EC2 instances",
            "- All VPCs and networking",
            "- All Elastic IPs",
            "- All Key Pairs",
            "- All available EBS volumes",
            "- GitHub SSH keys (Secrets Manager + GitHub)",
        ])
    if "nebius" in targets:
        items.extend([
            "- All Nebius compute instances",
            "- All Nebius disks",
            "- All Nebius shared filesystems",
        ])

    console.print(Panel.fit(
        "[bold red]EXTREME DANGER[/bold red]\n\n"
        f"Providers: {', '.join(targets)}\n\n"
        "This will PERMANENTLY DELETE:\n"
        + "\n".join(items) +
        "\n\n[bold]This action cannot be undone![/bold]",
        title="[bold red]NUCLEAR DELETION WARNING[/bold red]",
        border_style="red"
    ))

    if not force:
        confirmation = console.input("\n[bold red]Type 'DESTROY EVERYTHING' to confirm: [/bold red]")
        if confirmation != "DESTROY EVERYTHING":
            console.print("[yellow]Aborted. Nothing was deleted.[/yellow]")
            return

    aws_stats = {}
    nebius_stats = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:

        task = progress.add_task("[red]Starting...[/red]", total=None)

        # --- AWS ---
        if "aws" in targets:
            regions = [region] if region else get_regions()

            progress.update(task, description=f"[red]Terminating EC2 instances across {len(regions)} regions...[/red]")
            all_terminated = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(terminate_instances, r) for r in regions]
                for future in as_completed(futures):
                    all_terminated.extend(future.result())

            if all_terminated:
                console.print(f"\n[red]Terminated {len(all_terminated)} instances[/red]")
                for instance_id, name, r in all_terminated[:10]:
                    console.print(f"  - {instance_id} ({name}) in {r}")
                if len(all_terminated) > 10:
                    console.print(f"  ... and {len(all_terminated) - 10} more")
                progress.update(task, description="[yellow]Waiting for instances to terminate...[/yellow]")
                time.sleep(30)

            progress.update(task, description="[red]Deleting VPCs and networking...[/red]")
            all_vpcs = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(delete_vpcs, r) for r in regions]
                for future in as_completed(futures):
                    all_vpcs.extend(future.result())
            if all_vpcs:
                console.print(f"\n[red]Deleted {len(all_vpcs)} VPCs[/red]")

            progress.update(task, description="[red]Releasing Elastic IPs...[/red]")
            all_eips = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(release_elastic_ips, r) for r in regions]
                for future in as_completed(futures):
                    all_eips.extend(future.result())

            progress.update(task, description="[red]Deleting key pairs...[/red]")
            all_keys = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(delete_key_pairs, r) for r in regions]
                for future in as_completed(futures):
                    all_keys.extend(future.result())

            progress.update(task, description="[red]Deleting available EBS volumes...[/red]")
            all_volumes = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(delete_volumes, r) for r in regions]
                for future in as_completed(futures):
                    all_volumes.extend(future.result())

            progress.update(task, description="[red]Cleaning up secrets and SSH keys...[/red]")
            all_github_ssh = delete_github_ssh(regions[0])

            import boto3
            sm = boto3.client("secretsmanager", region_name=regions[0])
            for secret_name in ["brr-ec2-ssh-key"]:
                try:
                    sm.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
                    console.print(f"  Deleted Secrets Manager secret: [red]{secret_name}[/red]")
                except sm.exceptions.ResourceNotFoundException:
                    pass

            aws_stats = {
                "instances": len(all_terminated),
                "vpcs": len(all_vpcs),
                "eips": len(all_eips),
                "keys": len(all_keys),
                "volumes": len(all_volumes),
                "github_ssh": len(all_github_ssh),
            }

        # --- Nebius ---
        if "nebius" in targets:
            nebius_stats = _nuke_nebius(config["NEBIUS_PROJECT_ID"], progress, task)

        # Clean up local SSH config entries
        progress.update(task, description="[red]Cleaning up local SSH config...[/red]")
        from brr.aws.nodes import remove_ssh_config
        import re
        ssh_config_path = os.path.expanduser("~/.ssh/config")
        if os.path.exists(ssh_config_path):
            with open(ssh_config_path) as f:
                content = f.read()
            for alias in re.findall(r"^Host (brr-\S+)", content, re.MULTILINE):
                remove_ssh_config(alias)

        progress.update(task, description="[bold green]Nuclear deletion complete![/bold green]")

    # Summary
    lines = []
    if aws_stats:
        lines.append("[bold]AWS:[/bold]")
        lines.append(f"  Instances terminated: {aws_stats['instances']}")
        lines.append(f"  VPCs deleted: {aws_stats['vpcs']}")
        lines.append(f"  Elastic IPs released: {aws_stats['eips']}")
        lines.append(f"  Key pairs deleted: {aws_stats['keys']}")
        lines.append(f"  Volumes deleted: {aws_stats['volumes']}")
    if nebius_stats:
        lines.append("[bold]Nebius:[/bold]")
        lines.append(f"  Instances terminated: {nebius_stats['instances']}")
        lines.append(f"  Disks deleted: {nebius_stats['disks']}")
        lines.append(f"  Filesystems deleted: {nebius_stats['filesystems']}")

    console.print("\n" + "=" * 50)
    console.print(Panel.fit(
        "[bold red]DESTRUCTION COMPLETE[/bold red]\n\n"
        + "\n".join(lines),
        title="[bold]Final Report[/bold]",
        border_style="red"
    ))
