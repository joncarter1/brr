"""Nebius instance query helpers for brr."""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


# Nebius Instance state enum (protobuf integers):
#   UNSPECIFIED=0, CREATING=1, UPDATING=2, STARTING=3,
#   RUNNING=4, STOPPING=5, STOPPED=6, DELETING=7, ERROR=8
_RUNNING_STATES = {4, "4", "RUNNING"}
_STOPPED_STATES = {5, "5", 6, "6", "STOPPED", "STOPPING"}
_TERMINAL_STATES = {7, "7", 8, "8", "ERROR", "DELETING"}


def _is_running(state):
    return state in _RUNNING_STATES or "RUNNING" in str(state)


def _is_stopped(state):
    return state in _STOPPED_STATES or any(s in str(state) for s in ("STOPPED", "STOPPING"))


def _is_terminated(state):
    return state in _TERMINAL_STATES or any(s in str(state) for s in ("ERROR", "DELETING"))


def _nebius_sdk():
    from nebius.sdk import SDK

    creds_file = Path.home() / ".nebius" / "credentials.json"
    if creds_file.exists():
        return SDK(credentials_file_name=str(creds_file))
    return SDK()


def _extract_ip(instance, public=True):
    """Extract IP address from a Nebius instance."""
    if not instance or not instance.status:
        return None
    interfaces = instance.status.network_interfaces
    if not interfaces:
        return None
    iface = interfaces[0]
    if public:
        addr = getattr(iface, "public_ip_address", None)
    else:
        addr = getattr(iface, "ip_address", None)
    if addr and addr.address:
        return addr.address.split("/")[0]
    return None


def _format_uptime(created_at):
    """Return human-readable uptime from a created_at timestamp."""
    if not created_at:
        return "-"
    try:
        now = datetime.now(timezone.utc)
        if hasattr(created_at, "seconds"):
            # protobuf Timestamp
            dt = datetime.fromtimestamp(created_at.seconds, tz=timezone.utc)
        elif isinstance(created_at, datetime):
            dt = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        else:
            return "-"
        diff = now - dt
        days = diff.days
        hours, remainder = divmod(diff.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return "-"


def query_head_ip(project_id, cluster_name):
    """Find the public IP of the head node for a Nebius Ray cluster.

    Queries instances by ray-cluster-name label and returns the head node's
    public IP, or None if not found.
    """
    return asyncio.run(_query_head_ip(project_id, cluster_name))


async def _query_head_ip(project_id, cluster_name):
    from nebius.api.nebius.compute.v1 import (
        InstanceServiceClient,
        ListInstancesRequest,
    )

    sdk = _nebius_sdk()
    async with sdk:
        client = InstanceServiceClient(sdk)
        response = await client.list(ListInstancesRequest(parent_id=project_id))

        for inst in response.items:
            labels = dict(inst.metadata.labels) if inst.metadata.labels else {}
            if labels.get("ray-cluster-name") != cluster_name:
                continue
            if labels.get("ray-node-type") != "head":
                continue

            state = inst.status.state if inst.status else None
            if not _is_running(state):
                continue

            return _extract_ip(inst, public=True)

    return None


def query_clusters(project_id):
    """Query Nebius for Ray clusters.

    Returns list of dicts with cluster_name, state, head_ip, preset,
    node_count, uptime — matching the format of aws/nodes.py:query_ray_clusters().
    """
    return asyncio.run(_query_clusters(project_id))


async def _query_clusters(project_id):
    from nebius.api.nebius.compute.v1 import (
        InstanceServiceClient,
        ListInstancesRequest,
    )

    sdk = _nebius_sdk()
    async with sdk:
        client = InstanceServiceClient(sdk)
        response = await client.list(ListInstancesRequest(parent_id=project_id))

        clusters = defaultdict(list)
        for inst in response.items:
            labels = dict(inst.metadata.labels) if inst.metadata.labels else {}
            cluster_name = labels.get("ray-cluster-name")
            if not cluster_name:
                continue

            state = inst.status.state if inst.status else None
            if _is_terminated(state):
                continue

            node_type = labels.get("ray-node-type", "worker")
            preset = getattr(inst.spec.resources, "preset", "-") if inst.spec else "-"
            created_at = getattr(inst.metadata, "created_at", None)

            clusters[cluster_name].append({
                "state": "running" if _is_running(state) else "stopped",
                "instance_type": str(preset),
                "public_ip": _extract_ip(inst, public=True),
                "node_type": node_type,
                "created_at": created_at,
                "instance_id": inst.metadata.id,
            })

        result = []
        for name, instances in sorted(clusters.items()):
            running = [i for i in instances if i["state"] == "running"]
            active = running if running else instances
            cluster_state = "running" if running else "stopped"

            head = next((i for i in active if i["node_type"] == "head"), None)

            result.append({
                "cluster_name": name,
                "state": cluster_state,
                "head_ip": (head["public_ip"] or "-") if head else "-",
                "instance_type": head["instance_type"] if head else active[0]["instance_type"],
                "node_count": len(active),
                "uptime": _format_uptime(head["created_at"]) if head and cluster_state == "running" else "-",
            })

        return result


def query_stopped_instances(project_id, cluster_name=None):
    """List stopped Nebius instances, optionally filtered by cluster.

    Returns list of dicts with instance_id, name, cluster_name.
    """
    return asyncio.run(_query_stopped_instances(project_id, cluster_name))


async def _query_stopped_instances(project_id, cluster_name=None):
    from nebius.api.nebius.compute.v1 import (
        InstanceServiceClient,
        ListInstancesRequest,
    )

    sdk = _nebius_sdk()
    async with sdk:
        client = InstanceServiceClient(sdk)
        response = await client.list(ListInstancesRequest(parent_id=project_id))

        stopped = []
        for inst in response.items:
            labels = dict(inst.metadata.labels) if inst.metadata.labels else {}
            inst_cluster = labels.get("ray-cluster-name")
            if not inst_cluster:
                continue
            if cluster_name and inst_cluster != cluster_name:
                continue

            state = inst.status.state if inst.status else None
            if not _is_stopped(state):
                continue

            stopped.append({
                "instance_id": inst.metadata.id,
                "name": inst.metadata.name or inst.metadata.id,
                "cluster_name": inst_cluster,
            })

        return stopped


def terminate_instances(project_id, instance_ids):
    """Terminate Nebius instances by ID. Returns count terminated."""
    return asyncio.run(_terminate_instances(instance_ids))


def terminate_cluster_instances(project_id, cluster_name):
    """Terminate all instances for a Nebius Ray cluster. Returns count terminated."""
    return asyncio.run(_terminate_cluster_instances(project_id, cluster_name))


async def _terminate_instances(instance_ids):
    from nebius.api.nebius.compute.v1 import (
        InstanceServiceClient,
        DeleteInstanceRequest,
    )

    sdk = _nebius_sdk()
    async with sdk:
        client = InstanceServiceClient(sdk)
        count = 0
        for iid in instance_ids:
            try:
                op = await client.delete(DeleteInstanceRequest(id=iid))
                await op.wait()
                count += 1
            except Exception:
                pass
        return count


async def _terminate_cluster_instances(project_id, cluster_name):
    from nebius.api.nebius.compute.v1 import (
        InstanceServiceClient,
        ListInstancesRequest,
        DeleteInstanceRequest,
    )

    sdk = _nebius_sdk()
    async with sdk:
        client = InstanceServiceClient(sdk)
        response = await client.list(ListInstancesRequest(parent_id=project_id))

        ids = []
        for inst in response.items:
            labels = dict(inst.metadata.labels) if inst.metadata.labels else {}
            if labels.get("ray-cluster-name") != cluster_name:
                continue
            state = inst.status.state if inst.status else None
            if _is_terminated(state):
                continue
            ids.append(inst.metadata.id)

        count = 0
        for iid in ids:
            try:
                op = await client.delete(DeleteInstanceRequest(id=iid))
                await op.wait()
                count += 1
            except Exception:
                pass
        return count
