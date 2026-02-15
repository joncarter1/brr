import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime

import click
from rich.console import Console
from rich.panel import Panel

from brr.state import ensure_state_dirs, read_config, write_config, CONFIG_PATH, KEYS_DIR

console = Console()

DEFAULTS = {
    "AWS_REGION": "us-east-1",
    "AMI_UBUNTU": "ami-0360c520857e3138f",
    "AMI_DL": "ami-0b594b8835777de74",
}


def get_or_create_key(ec2, region):
    """Find an existing EC2 key pair or create a new one."""
    ensure_state_dirs()

    local_keys = [f for f in os.listdir(KEYS_DIR) if f.endswith(".pem")]
    if local_keys:
        aws_keys_resp = ec2.describe_key_pairs()
        aws_key_names = [k['KeyName'] for k in aws_keys_resp['KeyPairs']]

        for lk in local_keys:
            key_name = lk.replace(".pem", "")
            if key_name in aws_key_names:
                console.print(f"Using existing local key: [green]{lk}[/green]")
                return key_name, str(KEYS_DIR / lk)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    key_name = f"brr-{region}-{timestamp}"
    key_file = str(KEYS_DIR / f"{key_name}.pem")

    console.print(f"Generating new key pair: [bold cyan]{key_name}[/bold cyan]...")
    resp = ec2.create_key_pair(KeyName=key_name)

    with open(key_file, "w") as f:
        f.write(resp['KeyMaterial'])

    os.chmod(key_file, stat.S_IRUSR)

    return key_name, key_file


def get_default_vpc(ec2):
    """Find the default VPC, or the first available VPC."""
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    if vpcs["Vpcs"]:
        return vpcs["Vpcs"][0]["VpcId"]
    vpcs = ec2.describe_vpcs()
    if vpcs["Vpcs"]:
        return vpcs["Vpcs"][0]["VpcId"]
    return None


def get_or_create_cluster_sg(ec2, vpc_id):
    """Create or find the brr-cluster security group with SSH + cluster mesh rules."""
    sg_name = "brr-cluster"

    try:
        resp = ec2.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [sg_name]},
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]
        )
        if resp["SecurityGroups"]:
            sg_id = resp["SecurityGroups"][0]["GroupId"]
            console.print(f"Using existing security group: [green]{sg_name}[/green] ({sg_id})")
            return sg_id
    except Exception:
        pass

    try:
        console.print(f"Creating security group: [bold cyan]{sg_name}[/bold cyan]...")
        resp = ec2.create_security_group(
            GroupName=sg_name,
            Description="Ray cluster - SSH + cluster mesh",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]

        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
                {
                    "IpProtocol": "-1",
                    "UserIdGroupPairs": [{"GroupId": sg_id}],
                },
            ],
        )
        console.print(f"Created security group: [green]{sg_id}[/green]")
        return sg_id
    except Exception as e:
        console.print(f"[red]Error creating security group: {e}[/red]")
        return None


def _wait_for_efs(efs_client, fs_id, timeout=120):
    """Poll until EFS filesystem is available."""
    for _ in range(timeout // 5):
        resp = efs_client.describe_file_systems(FileSystemId=fs_id)
        state = resp["FileSystems"][0]["LifeCycleState"]
        if state == "available":
            return
        time.sleep(5)
    raise TimeoutError(f"EFS {fs_id} did not become available within {timeout}s")


def _ensure_mount_targets(efs_client, ec2, fs_id, vpc_id, sg_id):
    """Create mount targets in all AZs of the VPC that don't already have one."""
    existing = efs_client.describe_mount_targets(FileSystemId=fs_id)
    existing_azs = {mt["AvailabilityZoneName"] for mt in existing["MountTargets"]}

    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    az_to_subnet = {}
    for subnet in subnets["Subnets"]:
        az = subnet["AvailabilityZone"]
        if az not in az_to_subnet:
            az_to_subnet[az] = subnet["SubnetId"]

    created = []
    for az, subnet_id in az_to_subnet.items():
        if az in existing_azs:
            continue
        try:
            efs_client.create_mount_target(
                FileSystemId=fs_id,
                SubnetId=subnet_id,
                SecurityGroups=[sg_id],
            )
            created.append(az)
        except efs_client.exceptions.MountTargetConflict:
            pass

    if created:
        console.print(f"Created EFS mount targets in: [green]{', '.join(created)}[/green]")
        for _ in range(60):
            resp = efs_client.describe_mount_targets(FileSystemId=fs_id)
            states = [mt["LifeCycleState"] for mt in resp["MountTargets"]]
            if all(s == "available" for s in states):
                return
            time.sleep(5)
        console.print("[yellow]Warning: some mount targets may still be initializing[/yellow]")
    else:
        console.print("EFS mount targets already exist in all AZs")


def get_or_create_efs(efs_client, ec2, vpc_id, sg_id):
    """Create or find the brr-shared EFS filesystem with mount targets."""
    existing = efs_client.describe_file_systems(CreationToken="brr-shared")
    if existing["FileSystems"]:
        fs = existing["FileSystems"][0]
        fs_id = fs["FileSystemId"]
        console.print(f"Using existing EFS: [green]{fs_id}[/green]")
        if fs["LifeCycleState"] != "available":
            _wait_for_efs(efs_client, fs_id)
    else:
        console.print("Creating EFS filesystem: [bold cyan]brr-shared[/bold cyan]...")
        resp = efs_client.create_file_system(
            CreationToken="brr-shared",
            PerformanceMode="generalPurpose",
            ThroughputMode="elastic",
            Encrypted=True,
            Tags=[{"Key": "Name", "Value": "brr-shared"}],
        )
        fs_id = resp["FileSystemId"]
        console.print(f"Created EFS: [green]{fs_id}[/green]")
        _wait_for_efs(efs_client, fs_id)

    _ensure_mount_targets(efs_client, ec2, fs_id, vpc_id, sg_id)

    return fs_id


def setup_github_ssh(region, key_path):
    """Upload EC2 SSH key to Secrets Manager and add public key to GitHub."""
    import boto3
    secret_name = "brr-github-ssh-key"
    sm = boto3.client("secretsmanager", region_name=region)

    try:
        with open(key_path) as f:
            private_key = f.read()
        sm.create_secret(Name=secret_name, SecretString=private_key)
        console.print(f"Uploaded SSH key to Secrets Manager: [green]{secret_name}[/green]")
    except sm.exceptions.ResourceExistsException:
        console.print(f"SSH key already in Secrets Manager: [green]{secret_name}[/green]")
    except sm.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "AccessDeniedException":
            console.print(f"[red]Access denied for secretsmanager:CreateSecret[/red]")
            console.print("[yellow]Attach the SecretsManager IAM policy from the README to your IAM user,[/yellow]")
            console.print("[yellow]then re-run 'brr configure aws'.[/yellow]")
            return ""
        raise

    if not shutil.which("gh"):
        console.print("[yellow]gh CLI not found — skipping GitHub key setup[/yellow]")
        console.print("[yellow]Install gh and run 'brr configure aws' again to add the key to GitHub[/yellow]")
        return secret_name

    auth_check = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True
    )
    if auth_check.returncode != 0:
        console.print("[yellow]gh CLI not authenticated — skipping GitHub key setup[/yellow]")
        console.print("[yellow]Run 'gh auth login' and then 'brr configure aws' again[/yellow]")
        return secret_name

    list_result = subprocess.run(
        ["gh", "ssh-key", "list"], capture_output=True, text=True
    )
    if list_result.returncode == 0:
        for line in list_result.stdout.splitlines():
            if "brr-aws" in line:
                console.print("SSH key already registered on GitHub: [green]brr-aws[/green]")
                return secret_name

    pubkey_result = subprocess.run(
        ["ssh-keygen", "-y", "-f", key_path], capture_output=True, text=True
    )
    if pubkey_result.returncode != 0:
        console.print(f"[red]Failed to derive public key: {pubkey_result.stderr.strip()}[/red]")
        return secret_name

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False) as tmp:
        tmp.write(pubkey_result.stdout)
        tmp_path = tmp.name

    try:
        add_result = subprocess.run(
            ["gh", "ssh-key", "add", tmp_path, "--title", "brr-aws"],
            capture_output=True, text=True,
        )
        if add_result.returncode == 0:
            console.print("Added SSH key to GitHub: [green]brr-aws[/green]")
        else:
            console.print(f"[red]Failed to add key to GitHub: {add_result.stderr.strip()}[/red]")
    finally:
        os.unlink(tmp_path)

    _attach_secretsmanager_policy(region)

    return secret_name



def _store_ec2_ssh_key(region, key_path):
    """Store the EC2 SSH private key in Secrets Manager so cluster nodes can fetch it."""
    import boto3
    secret_name = "brr-ec2-ssh-key"
    sm = boto3.client("secretsmanager", region_name=region)

    with open(key_path) as f:
        private_key = f.read()

    try:
        sm.create_secret(Name=secret_name, SecretString=private_key)
        console.print(f"Stored EC2 SSH key in Secrets Manager: [green]{secret_name}[/green]")
    except sm.exceptions.ResourceExistsException:
        sm.put_secret_value(SecretId=secret_name, SecretString=private_key)
        console.print(f"Updated EC2 SSH key in Secrets Manager: [green]{secret_name}[/green]")
    except sm.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "AccessDeniedException":
            console.print("[red]Access denied for secretsmanager:CreateSecret[/red]")
            console.print("[yellow]Attach the SecretsManager IAM policy from the README.[/yellow]")
            return ""
        raise

    return secret_name


def _attach_secretsmanager_policy(region):
    """Attach Secrets Manager read policy to ray-autoscaler-v1 role."""
    import boto3
    iam = boto3.client("iam")
    try:
        iam.put_role_policy(
            RoleName="ray-autoscaler-v1",
            PolicyName="brr-secretsmanager-read",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": "secretsmanager:GetSecretValue",
                    "Resource": f"arn:aws:secretsmanager:{region}:*:secret:brr-*"
                }]
            }),
        )
        console.print(f"Added Secrets Manager permission to [green]ray-autoscaler-v1[/green] role")
    except iam.exceptions.NoSuchEntityException:
        console.print("[yellow]IAM role 'ray-autoscaler-v1' not found yet (created on first cluster launch).[/yellow]")
        console.print("[yellow]Permission will be added automatically on next `brr up`.[/yellow]")


def _attach_iam_passrole_policy():
    """Attach IAM PassRole/GetInstanceProfile policy to ray-autoscaler-v1.

    Ray's autoscaler needs these to launch new instances with the same
    instance profile.
    """
    import boto3
    iam = boto3.client("iam")
    try:
        iam.put_role_policy(
            RoleName="ray-autoscaler-v1",
            PolicyName="brr-iam-passrole",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "iam:GetInstanceProfile",
                            "iam:PassRole",
                        ],
                        "Resource": [
                            "arn:aws:iam::*:instance-profile/ray-autoscaler-v1",
                            "arn:aws:iam::*:role/ray-autoscaler-v1",
                        ],
                    },
                ],
            }),
        )
        console.print(f"Added IAM PassRole permission to [green]ray-autoscaler-v1[/green] role")
    except iam.exceptions.NoSuchEntityException:
        console.print("[yellow]IAM role 'ray-autoscaler-v1' not found yet.[/yellow]")


def _attach_ssm_policy():
    """Attach SSM managed policy to ray-autoscaler-v1 for Session Manager access."""
    import boto3
    iam = boto3.client("iam")
    try:
        iam.attach_role_policy(
            RoleName="ray-autoscaler-v1",
            PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        )
        console.print(f"Added SSM Session Manager permission to [green]ray-autoscaler-v1[/green] role")
    except iam.exceptions.NoSuchEntityException:
        console.print("[yellow]IAM role 'ray-autoscaler-v1' not found yet.[/yellow]")


def configure_aws():
    """Interactive AWS configuration wizard."""
    ensure_state_dirs()

    existing = read_config()

    console.print(Panel("AWS configuration", title="brr configure", border_style="cyan"))

    region = click.prompt(
        "AWS region",
        default=existing.get("AWS_REGION", DEFAULTS["AWS_REGION"]),
    )
    ami_ubuntu = click.prompt(
        "Ubuntu AMI",
        default=existing.get("AMI_UBUNTU", DEFAULTS["AMI_UBUNTU"]),
    )
    ami_dl = click.prompt(
        "Deep Learning AMI",
        default=existing.get("AMI_DL", DEFAULTS["AMI_DL"]),
    )

    efs_enabled = click.confirm(
        "Enable shared EFS filesystem?",
        default=bool(existing.get("EFS_ID", "")),
    )

    console.print()

    import boto3
    from botocore.exceptions import NoCredentialsError, PartialCredentialsError

    ec2 = boto3.client("ec2", region_name=region)

    try:
        vpc_id = get_default_vpc(ec2)
    except (NoCredentialsError, PartialCredentialsError):
        console.print("[red]AWS credentials not found.[/red]")
        console.print("Run [bold]aws configure[/bold] to set up your credentials first.")
        console.print("See: https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html")
        raise click.Abort()

    if not vpc_id:
        console.print("[red]No VPC found[/red]")
        raise click.Abort()

    sg_id = get_or_create_cluster_sg(ec2, vpc_id)
    if not sg_id:
        raise click.Abort()

    key_name, key_path = get_or_create_key(ec2, region)
    ec2_ssh_secret = _store_ec2_ssh_key(region, key_path)
    _attach_iam_passrole_policy()
    _attach_ssm_policy()

    efs_id = ""
    if efs_enabled:
        efs_client = boto3.client("efs", region_name=region)
        efs_id = get_or_create_efs(efs_client, ec2, vpc_id, sg_id)

    github_ssh_enabled = click.confirm(
        "Set up GitHub SSH access for clusters?",
        default=bool(existing.get("GITHUB_SSH_SECRET", "")),
    )
    github_ssh_secret = ""
    if github_ssh_enabled:
        github_ssh_secret = setup_github_ssh(region, key_path)

    updates = {
        "AWS_REGION": region,
        "AWS_SECURITY_GROUP": sg_id,
        "AWS_KEY_NAME": key_name,
        "AWS_SSH_KEY": key_path,
        "EFS_ID": efs_id,
        "AMI_UBUNTU": ami_ubuntu,
        "AMI_DL": ami_dl,
        "GITHUB_SSH_SECRET": github_ssh_secret,
        "EC2_SSH_SECRET": ec2_ssh_secret,
    }

    merged = dict(existing)
    merged.update(updates)
    write_config(merged)
    console.print(f"\nWrote [green]{CONFIG_PATH}[/green]")

    console.print()
    console.print("[bold green]Done![/bold green] Next steps:")
    console.print("  brr configure tools                         # select AI coding tools")
    console.print("  brr configure general                       # instance settings")
    console.print("  brr up aws:cpu                              # launch CPU cluster")
