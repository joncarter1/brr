"""Verda instance query helpers for brr."""

import configparser
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path


VERDA_QUERY_TIMEOUT = 30

VERDA_CREDENTIALS_PATH = Path.home() / ".verda" / "credentials"
VERDA_CONFIG_PATH = Path.home() / ".verda" / "config.yaml"

# Verda instance status enum (string values from the REST API):
#   running, provisioning, offline, discontinued, unknown, ordered,
#   notfound, new, error, deleting, validating, no_capacity,
#   installation_failed, hibernated, hibernating
_RUNNING_STATES = {"running"}
_STOPPED_STATES = {"offline", "hibernating", "hibernated"}
_TERMINAL_STATES = {
    "deleting", "discontinued", "error",
    "installation_failed", "no_capacity", "notfound",
}


def _verda_credentials():
    """Load (client_id, client_secret) from env vars or ~/.verda/credentials.

    Returns a tuple or raises RuntimeError if neither source has credentials.
    """
    env_id = os.environ.get("VERDA_CLIENT_ID")
    env_secret = os.environ.get("VERDA_CLIENT_SECRET")
    if env_id and env_secret:
        return env_id, env_secret

    if VERDA_CREDENTIALS_PATH.exists():
        profile = _active_profile()
        parser = configparser.ConfigParser()
        parser.read(VERDA_CREDENTIALS_PATH)
        if parser.has_section(profile):
            cid = parser.get(profile, "verda_client_id", fallback="")
            secret = parser.get(profile, "verda_client_secret", fallback="")
            if cid and secret:
                return cid, secret

    raise RuntimeError(
        "No Verda credentials found. Set VERDA_CLIENT_ID/VERDA_CLIENT_SECRET "
        "or run `verda auth login`."
    )


def _active_profile():
    """Return the active profile name from ~/.verda/config.yaml. Defaults to 'default'."""
    if not VERDA_CONFIG_PATH.exists():
        return "default"
    try:
        content = VERDA_CONFIG_PATH.read_text()
        match = re.search(r"^\s*active_profile\s*:\s*(\S+)\s*$", content, re.MULTILINE)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    return "default"


def _verda_client():
    """Return a VerdaClient built from env vars or ~/.verda/credentials."""
    from verda import VerdaClient

    client_id, client_secret = _verda_credentials()
    base_url = os.environ.get("VERDA_BASE_URL", "https://api.verda.com/v1")
    return VerdaClient(client_id, client_secret, base_url=base_url)


def _run_with_timeout(fn, *args, **kwargs):
    with ThreadPoolExecutor(max_workers=1) as exe:
        fut = exe.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout=VERDA_QUERY_TIMEOUT)
        except FuturesTimeout:
            raise TimeoutError(
                f"Verda query timed out after {VERDA_QUERY_TIMEOUT}s — check network connection"
            ) from None


def _is_running(state):
    return str(state).lower() in _RUNNING_STATES


def _is_stopped(state):
    return str(state).lower() in _STOPPED_STATES


def _is_terminated(state):
    return str(state).lower() in _TERMINAL_STATES


def _parse_description(instance):
    """Extract the compact ``c=...;u=...;k=...`` metadata brr writes into description.

    Returns dict with keys ``c`` (cluster), ``u`` (user_node_type), ``k`` (kind)
    when present, else {}.
    """
    desc = getattr(instance, "description", "") or ""
    parsed = {}
    if not desc or "=" not in desc:
        return parsed
    for part in desc.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


def _extract_cluster_name(instance):
    """Return the brr cluster name for a Verda instance, or None."""
    meta = _parse_description(instance)
    cluster = meta.get("c")
    if cluster:
        return cluster

    # Fallback: hostname format is "brr-{cluster}-{node_type}-{uuid8}".
    hostname = getattr(instance, "hostname", "") or ""
    if hostname.startswith("brr-"):
        parts = hostname[4:].rsplit("-", 2)
        if len(parts) >= 3:
            return parts[0]
    return None


def _extract_node_type(instance):
    """Return 'head' | 'worker' for the instance, or 'worker' by default."""
    meta = _parse_description(instance)
    nt = meta.get("k")
    if nt in ("head", "worker"):
        return nt
    return "worker"


def _format_uptime(created_at):
    """Return human-readable uptime from an ISO8601 created_at string."""
    if not created_at:
        return "-"
    try:
        s = str(created_at).rstrip("Z")
        # Normalize fractional seconds / timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - dt
        days = diff.days
        hours, remainder = divmod(diff.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return "-"


def query_head_ip(cluster_name):
    """Return the public IP of the head node for a Verda Ray cluster, or None."""
    return _run_with_timeout(_query_head_ip, cluster_name)


def _query_head_ip(cluster_name):
    client = _verda_client()
    for inst in client.instances.get():
        if _extract_cluster_name(inst) != cluster_name:
            continue
        if _extract_node_type(inst) != "head":
            continue
        if not _is_running(getattr(inst, "status", None)):
            continue
        ip = getattr(inst, "ip", None)
        if ip:
            return ip
    return None


def query_clusters(*_args, **_kwargs):
    """Return list of Verda cluster dicts.

    Each dict: cluster_name, state, head_ip, instance_type, node_count, uptime.
    Positional args are accepted and ignored for signature symmetry with the
    Nebius provider, which takes a project_id.
    """
    return _run_with_timeout(_query_clusters)


def _query_clusters():
    client = _verda_client()
    clusters = defaultdict(list)
    for inst in client.instances.get():
        name = _extract_cluster_name(inst)
        if not name:
            continue
        state = getattr(inst, "status", "") or ""
        if _is_terminated(state):
            continue
        clusters[name].append({
            "state": "running" if _is_running(state) else "stopped",
            "instance_type": getattr(inst, "instance_type", "-") or "-",
            "public_ip": getattr(inst, "ip", None) or None,
            "node_type": _extract_node_type(inst),
            "created_at": getattr(inst, "created_at", None),
            "instance_id": getattr(inst, "id", None),
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


def query_stopped_instances(*args, **_kwargs):
    """Return stopped Verda instances, optionally filtered by cluster name.

    Accepts an optional positional argument for cluster_name to match the
    Nebius signature (which takes project_id, cluster_name).
    """
    cluster_name = None
    if len(args) == 1:
        cluster_name = args[0]
    elif len(args) >= 2:
        cluster_name = args[1]
    return _run_with_timeout(_query_stopped_instances, cluster_name)


def _query_stopped_instances(cluster_name=None):
    client = _verda_client()
    result = []
    for inst in client.instances.get():
        inst_cluster = _extract_cluster_name(inst)
        if not inst_cluster:
            continue
        if cluster_name and inst_cluster != cluster_name:
            continue
        if not _is_stopped(getattr(inst, "status", None)):
            continue
        result.append({
            "instance_id": getattr(inst, "id", ""),
            "name": getattr(inst, "hostname", "") or getattr(inst, "id", ""),
            "cluster_name": inst_cluster,
        })
    return result


def terminate_instances(*args, **_kwargs):
    """Delete Verda instances by ID list. Returns count deleted.

    Accepts (project_id, ids) for Nebius signature symmetry, or just (ids).
    """
    if len(args) == 1:
        ids = args[0]
    elif len(args) >= 2:
        ids = args[1]
    else:
        raise TypeError("terminate_instances requires an instance_ids argument")
    return _run_with_timeout(_terminate_instances, list(ids))


def _terminate_instances(instance_ids):
    if not instance_ids:
        return 0
    client = _verda_client()
    count = 0
    for iid in instance_ids:
        try:
            client.instances.action(iid, action="delete")
            count += 1
        except Exception:
            pass
    return count


def terminate_cluster_instances(*args, **_kwargs):
    """Delete every Verda instance matching a cluster name. Returns count deleted.

    Accepts (project_id, cluster_name) for Nebius signature symmetry, or
    just (cluster_name).
    """
    if len(args) == 1:
        cluster_name = args[0]
    elif len(args) >= 2:
        cluster_name = args[1]
    else:
        raise TypeError("terminate_cluster_instances requires a cluster_name argument")
    return _run_with_timeout(_terminate_cluster_instances, cluster_name)


def _terminate_cluster_instances(cluster_name):
    client = _verda_client()
    ids = []
    for inst in client.instances.get():
        if _extract_cluster_name(inst) != cluster_name:
            continue
        state = getattr(inst, "status", "") or ""
        if _is_terminated(state):
            continue
        ids.append(getattr(inst, "id", None))
    ids = [i for i in ids if i]
    count = 0
    for iid in ids:
        try:
            client.instances.action(iid, action="delete")
            count += 1
        except Exception:
            pass
    return count
