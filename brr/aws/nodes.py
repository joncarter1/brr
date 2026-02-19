from collections import defaultdict
from datetime import datetime, timezone

from rich.console import Console

console = Console()



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


