import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

console = Console()


def ensure_secretsmanager_iam(region):
    """Silently ensure ray-autoscaler-v1 has Secrets Manager read access."""
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
        console.print("[dim]Ensured Secrets Manager IAM permission for ray-autoscaler-v1[/dim]")
    except Exception:
        pass  # Best effort — configure already handles the verbose path


def cleanup_stopped_instances(region, cluster_name):
    """Terminate stopped instances tagged with the given ray-cluster-name.

    Returns the number of instances terminated.
    """
    import boto3
    ec2 = boto3.client("ec2", region_name=region)
    paginator = ec2.get_paginator("describe_instances")
    pages = paginator.paginate(
        Filters=[
            {"Name": "tag:ray-cluster-name", "Values": [cluster_name]},
            {"Name": "instance-state-name", "Values": ["stopped"]},
        ]
    )
    ids = [
        inst["InstanceId"]
        for page in pages
        for res in page["Reservations"]
        for inst in res["Instances"]
    ]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
    return len(ids)


def terminate_cluster_instances(region, cluster_name):
    """Terminate all instances tagged with the given ray-cluster-name.

    Targets running, stopped, stopping, and pending instances.
    Returns the number of instances terminated.
    """
    import boto3
    ec2 = boto3.client("ec2", region_name=region)
    paginator = ec2.get_paginator("describe_instances")
    pages = paginator.paginate(
        Filters=[
            {"Name": "tag:ray-cluster-name", "Values": [cluster_name]},
            {"Name": "instance-state-name", "Values": [
                "running", "stopped", "stopping", "pending",
            ]},
        ]
    )
    ids = [
        inst["InstanceId"]
        for page in pages
        for res in page["Reservations"]
        for inst in res["Instances"]
    ]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
    return len(ids)


def get_ray_status(head_ip, ssh_key):
    """SSH into a head node and query Ray for node/resource status.

    Returns dict with 'nodes', 'cpu', 'gpu' or None on failure.
    """
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
        "-i", ssh_key, f"ubuntu@{head_ip}",
        'source ~/.venv/bin/activate && python -c "'
        "import ray, json; "
        "ray.init(address='auto', ignore_reinit_error=True); "
        "nodes = [n for n in ray.nodes() if n['Alive']]; "
        "res = ray.cluster_resources(); "
        "print(json.dumps({'nodes': len(nodes), "
        "'cpu': int(res.get('CPU', 0)), "
        "'gpu': int(res.get('GPU', 0))}))"
        '"',
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return json.loads(result.stdout.strip())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass
    return None


def format_uptime(launch_time):
    """Return human-readable uptime from a launch time datetime."""
    if not launch_time:
        return "-"
    diff = datetime.now(timezone.utc) - launch_time
    days = diff.days
    hours, remainder = divmod(diff.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def query_ray_clusters(region):
    """Query EC2 for instances tagged by Ray, grouped by cluster name."""
    import boto3
    ec2 = boto3.client("ec2", region_name=region)
    paginator = ec2.get_paginator("describe_instances")
    pages = paginator.paginate(
        Filters=[
            {"Name": "tag-key", "Values": ["ray-cluster-name"]},
            {"Name": "instance-state-name", "Values": [
                "running", "stopped", "stopping", "pending",
            ]},
        ]
    )

    clusters = defaultdict(list)
    for page in pages:
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                cluster_name = tags.get("ray-cluster-name", "unknown")
                clusters[cluster_name].append({
                    "state": instance["State"]["Name"],
                    "instance_type": instance.get("InstanceType", "-"),
                    "public_ip": instance.get("PublicIpAddress"),
                    "private_ip": instance.get("PrivateIpAddress"),
                    "launch_time": instance.get("LaunchTime"),
                    "node_type": tags.get("ray-node-type", "worker"),
                })

    result = []
    for name, all_instances in sorted(clusters.items()):
        # Prefer running instances; fall back to most recently launched stopped set
        running = [i for i in all_instances if i["state"] == "running"]
        if running:
            instances = running
            cluster_state = "running"
        else:
            # Keep only the most recently launched stopped head + its workers
            stopped = [i for i in all_instances if i["state"] == "stopped"]
            stopped.sort(key=lambda i: i["launch_time"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            if stopped:
                newest_head = next((i for i in stopped if i["node_type"] == "head"), None)
                if newest_head and newest_head["launch_time"]:
                    instances = [i for i in stopped if i["launch_time"] and i["launch_time"] >= newest_head["launch_time"]]
                else:
                    instances = stopped[:1]
            else:
                instances = all_instances
            cluster_state = "stopped"

        head = next((i for i in instances if i["node_type"] == "head"), None)

        result.append({
            "cluster_name": name,
            "state": cluster_state,
            "head_ip": (head["public_ip"] or head.get("private_ip") or "-") if head else "-",
            "instance_type": head["instance_type"] if head else instances[0]["instance_type"],
            "node_count": len(instances),
            "uptime": format_uptime(head["launch_time"]) if head and head["state"] == "running" else "-",
        })

    return result


def remove_ssh_config(host_alias):
    """Remove a Host block from ~/.ssh/config."""
    config_path = Path.home() / ".ssh" / "config"
    if not config_path.exists():
        return
    content = config_path.read_text()
    pattern = re.compile(
        rf"^Host {re.escape(host_alias)}\n(?:[ \t]+\S.*\n)*",
        re.MULTILINE,
    )
    new_content = pattern.sub("", content)
    if new_content != content:
        config_path.write_text(new_content)
        console.print(f"[dim]Removed SSH config: {host_alias}[/dim]")


def update_ssh_config(host_alias, head_ip, ssh_key):
    """Write or update a Host block in ~/.ssh/config.

    host_alias is the full SSH alias (e.g. 'brr-cpu', 'brr-nebius-h100').
    """
    block = (
        f"Host {host_alias}\n"
        f"    HostName {head_ip}\n"
        f"    User ubuntu\n"
        f"    IdentityFile {ssh_key}\n"
        f"    StrictHostKeyChecking accept-new\n"
        f"    ProxyCommand none\n"
    )

    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    config_path = ssh_dir / "config"

    if config_path.exists():
        content = config_path.read_text()
    else:
        content = ""

    # Replace existing block or append
    pattern = re.compile(
        rf"^Host {re.escape(host_alias)}\n(?:[ \t]+\S.*\n)*",
        re.MULTILINE,
    )
    if pattern.search(content):
        content = pattern.sub(block, content)
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n" + block

    config_path.write_text(content)
    os.chmod(config_path, 0o600)

    console.print(f"[dim]Updated SSH config: {host_alias} -> {head_ip}[/dim]")
    console.print(
        f'[dim]VS Code: Cmd+Shift+P -> "Remote-SSH: Connect to Host" -> {host_alias}[/dim]'
    )
