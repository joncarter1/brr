"""Custom Ray NodeProvider for Verda Cloud.

Uses the Verda Python SDK (REST over HTTPS) to manage compute instances.
Ray's autoscaler calls these methods to create/terminate/query nodes.

Verda instances have no native tag/label field, so Ray tags are
persisted via VerdaTagStore. Immutable identity (cluster name, user node
type) is also encoded in hostname + description so we can rehydrate the
tag store after loss.
"""

import logging
import os
import threading
import time
import uuid

from ray.autoscaler.node_provider import NodeProvider
from ray.autoscaler.tags import (
    TAG_RAY_CLUSTER_NAME,
    TAG_RAY_NODE_KIND,
    TAG_RAY_USER_NODE_TYPE,
)


logger = logging.getLogger(__name__)


_RUNNING_STATES = {"running"}
_STOPPED_STATES = {"offline", "hibernating", "hibernated"}
_TERMINAL_STATES = {
    "deleting", "discontinued", "error",
    "installation_failed", "no_capacity", "notfound",
}

_DESCRIPTION_MAX = 60


def _parse_description(desc):
    """Parse the compact ``c=...;u=...;k=...`` description format."""
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


def _encode_description(cluster_name, user_node_type, node_kind):
    """Return ``c=...;u=...;k=...`` packed into <60 chars for Verda's description field."""
    payload = f"c={cluster_name};u={user_node_type};k={node_kind}"
    if len(payload) <= _DESCRIPTION_MAX:
        return payload
    # Truncate the longest field first (cluster name, then user_node_type).
    # We prefer losing trailing chars over losing a whole field, so the
    # hostname and the local tag store still give us full identity.
    overflow = len(payload) - _DESCRIPTION_MAX
    if len(cluster_name) > overflow:
        trimmed = cluster_name[: len(cluster_name) - overflow]
        return f"c={trimmed};u={user_node_type};k={node_kind}"
    trimmed_ut = user_node_type[: max(0, len(user_node_type) - overflow)]
    return f"c=;u={trimmed_ut};k={node_kind}"[:_DESCRIPTION_MAX]


class VerdaNodeProvider(NodeProvider):
    """Ray NodeProvider backed by Verda Cloud's REST API."""

    _KNOWN_NODE_CONFIG_KEYS = {
        "instance_type", "location", "image", "ssh_key_ids",
        "startup_script_id", "is_spot", "contract", "pricing",
        "os_volume_size", "os_volume_name", "disk_size_gb",
        "storage_size", "storage_type", "storage_name",
    }

    def __init__(self, provider_config, cluster_name):
        super().__init__(provider_config, cluster_name)
        self.lock = threading.RLock()
        self._sdk_lock = threading.Lock()

        self.location = provider_config.get("location", "FIN-01")
        self.ssh_key_ids = self._unresolved_to_empty(
            provider_config.get("ssh_key_ids", []), list_ok=True
        )
        self.shared_volume_id = self._unresolved_to_empty(
            provider_config.get("shared_volume_id", "")
        )
        self.gpu_image = provider_config.get("gpu_image") or "ubuntu-24.04-cuda-12.8-open-docker"
        self.cpu_image = provider_config.get("cpu_image") or self.gpu_image
        self.cache_stopped_nodes = bool(provider_config.get("cache_stopped_nodes", False))
        self.stopped_node_action = provider_config.get("stopped_node_action", "shutdown")
        if self.stopped_node_action not in ("shutdown", "hibernate"):
            raise ValueError(
                f"stopped_node_action must be 'shutdown' or 'hibernate', "
                f"got {self.stopped_node_action!r}"
            )

        self._client = self._build_client(provider_config)

        from brr.verda.tag_store import VerdaTagStore
        self._tags = VerdaTagStore()

        # Hide in-flight creates/restarts from non_terminated_nodes so Ray's
        # updater doesn't race our provisioning call.
        self._hidden_hostnames = set()
        self._hidden_ids = set()

        # Instance cache: node_id -> verda.instances._instances.Instance
        self._cache = {}

        # Track which worker IDs we've already attached the shared volume to,
        # so we don't repeatedly call attach on the same worker across
        # autoscaler ticks.
        self._shared_attached = set()

    @staticmethod
    def _unresolved_to_empty(value, list_ok=False):
        """Strip unrendered ``{{PLACEHOLDER}}`` values.

        Templates may ship with placeholder strings that survive rendering
        if the user opted out of the feature. Treat those as "not set".
        """
        if list_ok and isinstance(value, list):
            return [v for v in value if isinstance(v, str) and "{{" not in v]
        if isinstance(value, str) and "{{" in value:
            return ""
        return value

    def _build_client(self, provider_config):
        from verda import VerdaClient

        client_id = (
            provider_config.get("client_id")
            or os.environ.get("VERDA_CLIENT_ID")
        )
        client_secret = (
            provider_config.get("client_secret")
            or os.environ.get("VERDA_CLIENT_SECRET")
        )
        if not client_id or not client_secret:
            from brr.verda.nodes import _verda_credentials
            client_id, client_secret = _verda_credentials()

        base_url = provider_config.get("base_url") or os.environ.get(
            "VERDA_BASE_URL", "https://api.verda.com/v1"
        )
        return VerdaClient(client_id, client_secret, base_url=base_url)

    def _sdk_call(self, fn, *args, **kwargs):
        """Invoke an SDK method under the shared lock.

        The Verda SDK wraps a ``requests.Session``; concurrent use is not
        guaranteed safe, so we serialize calls.
        """
        with self._sdk_lock:
            return fn(*args, **kwargs)

    # ------------------------------------------------------------------
    # Tag helpers
    # ------------------------------------------------------------------

    def _identity_tags(self, instance):
        """Derive immutable tags (cluster name, user node type) from an instance.

        Parses the description key=value format written at create time.
        Used when the local tag store is missing an entry.
        """
        tags = {TAG_RAY_CLUSTER_NAME: self.cluster_name}
        parsed = _parse_description(getattr(instance, "description", "") or "")
        user_nt = parsed.get("u")
        if user_nt:
            tags[TAG_RAY_USER_NODE_TYPE] = user_nt
        node_kind = parsed.get("k")
        if node_kind in ("head", "worker"):
            tags[TAG_RAY_NODE_KIND] = node_kind
        return tags

    def _read_tags(self, node_id, instance=None):
        stored = self._tags.get(node_id)
        if stored:
            return stored
        # Rehydrate immutables from the instance metadata.
        if instance is None:
            instance = self._fetch_instance(node_id)
        identity = self._identity_tags(instance) if instance is not None else {}
        identity.setdefault(TAG_RAY_CLUSTER_NAME, self.cluster_name)
        return identity

    # ------------------------------------------------------------------
    # Node queries
    # ------------------------------------------------------------------

    def non_terminated_nodes(self, tag_filters):
        with self.lock:
            hidden_ids = set(self._hidden_ids)
            hidden_hostnames = set(self._hidden_hostnames)

        prefix = f"brr-{self.cluster_name}-"
        instances = self._sdk_call(self._client.instances.get)

        result = []
        live_ids = set()
        for inst in instances:
            inst_id = getattr(inst, "id", None)
            if not inst_id:
                continue
            live_ids.add(inst_id)

            if inst_id in hidden_ids:
                continue
            hostname = getattr(inst, "hostname", "") or ""
            if hostname in hidden_hostnames:
                continue

            if not self._belongs_to_cluster(inst, prefix):
                continue

            state = getattr(inst, "status", "") or ""
            if self._is_terminal(state) or self._is_stopped(state):
                continue

            tags = self._read_tags(inst_id, inst)
            if not all(tags.get(k) == v for k, v in tag_filters.items()):
                continue

            with self.lock:
                self._cache[inst_id] = inst
            result.append(inst_id)

        # Prune tag-store entries for instances that no longer exist.
        try:
            self._tags.prune(live_ids)
        except OSError:
            logger.debug("Failed to prune Verda tag store", exc_info=True)

        return result

    def _belongs_to_cluster(self, instance, hostname_prefix):
        hostname = getattr(instance, "hostname", "") or ""
        if hostname.startswith(hostname_prefix):
            return True
        parsed = _parse_description(getattr(instance, "description", "") or "")
        return parsed.get("c") == self.cluster_name

    def is_running(self, node_id):
        inst = self._get_cached_or_fetch(node_id)
        if inst is None:
            return False
        return self._is_active(getattr(inst, "status", ""))

    def is_terminated(self, node_id):
        inst = self._get_cached_or_fetch(node_id)
        if inst is None:
            return True
        state = getattr(inst, "status", "")
        return self._is_terminal(state) or self._is_stopped(state)

    def node_tags(self, node_id):
        inst = self._cache.get(node_id)
        return self._read_tags(node_id, inst)

    def set_node_tags(self, node_id, tags):
        self._tags.update(node_id, tags)

    def external_ip(self, node_id):
        inst = self._get_cached_or_fetch(node_id)
        return getattr(inst, "ip", None) if inst is not None else None

    def internal_ip(self, node_id):
        # Verda's public API exposes only the public IP; expose it for
        # both internal and external to keep Ray happy.
        return self.external_ip(node_id)

    # ------------------------------------------------------------------
    # Node lifecycle
    # ------------------------------------------------------------------

    def create_node(self, node_config, tags, count):
        unknown = set(node_config) - self._KNOWN_NODE_CONFIG_KEYS
        if unknown:
            raise ValueError(
                f"Unknown node_config keys for Verda (typo?): {', '.join(sorted(unknown))}"
            )

        remaining = count
        user_node_type = tags.get(TAG_RAY_USER_NODE_TYPE)

        # Reuse stopped instances that match the user node type.
        if user_node_type:
            try:
                reusable = self._find_stopped_for_reuse(user_node_type)
            except Exception:
                logger.warning("Failed to query stopped Verda instances", exc_info=True)
                reusable = []

            for inst in reusable:
                if remaining <= 0:
                    break
                inst_id = getattr(inst, "id", None)
                if not inst_id:
                    continue
                with self.lock:
                    self._hidden_ids.add(inst_id)
                try:
                    self._sdk_call(self._client.instances.action, inst_id, action="start")
                    self._wait_for_status(inst_id, _RUNNING_STATES, timeout=300)
                    merged = dict(tags)
                    merged[TAG_RAY_CLUSTER_NAME] = self.cluster_name
                    self._tags.set(inst_id, merged)
                    self._ensure_shared_volume(inst_id)
                    fresh = self._sdk_call(self._client.instances.get_by_id, inst_id)
                    with self.lock:
                        self._cache[inst_id] = fresh
                    remaining -= 1
                    logger.info(f"Restarted stopped Verda instance {inst_id}")
                except Exception:
                    logger.warning(f"Failed to restart Verda instance {inst_id}",
                                   exc_info=True)
                finally:
                    with self.lock:
                        self._hidden_ids.discard(inst_id)

        # Create fresh instances for the remaining count.
        for _ in range(remaining):
            self._create_one(node_config, tags)

    def _create_one(self, node_config, tags):
        user_node_type = tags.get(TAG_RAY_USER_NODE_TYPE) or tags.get(TAG_RAY_NODE_KIND) or "worker"
        node_kind = tags.get(TAG_RAY_NODE_KIND) or "worker"
        sanitized = user_node_type.replace(".", "-").replace("_", "-").lower()
        hostname = f"brr-{self.cluster_name}-{sanitized}-{uuid.uuid4().hex[:8]}"
        # Verda hostnames must be valid DNS labels; keep to 63 chars.
        hostname = hostname[:63].rstrip("-")

        # Verda caps `description` at 60 chars. Encode immutable identity
        # as `c={cluster};u={user_node_type};k={kind}` — compact enough for
        # typical cluster/user-node-type names. Anything longer gets
        # truncated; we still have the hostname prefix + local tag store
        # as fallbacks.
        description = _encode_description(self.cluster_name, user_node_type, node_kind)

        instance_type = node_config["instance_type"]
        location = node_config.get("location") or self.location
        image = node_config.get("image") or self._default_image(instance_type)

        create_kwargs = {
            "instance_type": instance_type,
            "image": image,
            "hostname": hostname,
            "description": description,
            "ssh_key_ids": self._effective_ssh_key_ids(node_config),
            "location": location,
        }

        # OS volume size — accept either `os_volume_size` or the Nebius-style
        # `disk_size_gb` alias for template authoring symmetry.
        os_volume_size = node_config.get("os_volume_size") or node_config.get("disk_size_gb")
        if os_volume_size:
            create_kwargs["os_volume"] = {
                "name": node_config.get("os_volume_name") or f"{hostname}-os",
                "size": int(os_volume_size),
            }

        # Optional additional storage volume.
        storage_size = node_config.get("storage_size")
        if storage_size:
            create_kwargs["volumes"] = [{
                "name": node_config.get("storage_name") or f"{hostname}-storage",
                "size": int(storage_size),
                "type": node_config.get("storage_type") or "NVMe",
            }]

        if node_config.get("is_spot"):
            create_kwargs["is_spot"] = True
        contract = node_config.get("contract")
        if contract:
            create_kwargs["contract"] = contract
        pricing = node_config.get("pricing")
        if pricing:
            create_kwargs["pricing"] = pricing
        startup_script_id = node_config.get("startup_script_id")
        if startup_script_id and "{{" not in str(startup_script_id):
            create_kwargs["startup_script_id"] = startup_script_id

        with self.lock:
            self._hidden_hostnames.add(hostname)
        try:
            instance = self._sdk_call(self._client.instances.create, **create_kwargs)
            inst_id = getattr(instance, "id", None)
            if not inst_id:
                raise RuntimeError(f"Verda instance create returned no id: {instance!r}")
            logger.info(f"Created Verda instance {inst_id} ({hostname})")
        finally:
            with self.lock:
                self._hidden_hostnames.discard(hostname)

        # Ensure it's reached running state (SDK already waits, but the
        # default wait_for_status may not cover all cases).
        try:
            self._wait_for_status(inst_id, _RUNNING_STATES, timeout=600)
        except TimeoutError:
            logger.warning(f"Verda instance {inst_id} did not reach running state in time")

        merged_tags = dict(tags)
        merged_tags[TAG_RAY_CLUSTER_NAME] = self.cluster_name
        self._tags.set(inst_id, merged_tags)

        self._ensure_shared_volume(inst_id)

        fresh = self._sdk_call(self._client.instances.get_by_id, inst_id)
        with self.lock:
            self._cache[inst_id] = fresh

    def _effective_ssh_key_ids(self, node_config):
        keys = node_config.get("ssh_key_ids")
        if keys:
            keys = self._unresolved_to_empty(keys, list_ok=True)
            if keys:
                return keys
        return list(self.ssh_key_ids)

    def _default_image(self, instance_type):
        # CPU-only SKUs are prefixed "CPU." in Verda's taxonomy.
        if isinstance(instance_type, str) and instance_type.startswith("CPU."):
            return self.cpu_image
        return self.gpu_image

    def _ensure_shared_volume(self, node_id):
        """Attach the shared volume to node_id (idempotent, no-op if already attached)."""
        if not self.shared_volume_id:
            return
        if node_id in self._shared_attached:
            return
        try:
            self._sdk_call(
                self._client.volumes.attach,
                self.shared_volume_id,
                instance_id=node_id,
            )
            self._shared_attached.add(node_id)
            logger.info(f"Attached shared volume {self.shared_volume_id} to {node_id}")
        except Exception as e:
            # Verda returns an error if the volume is already attached — log
            # and continue; worst case the setup.sh mount step will succeed
            # without our help.
            msg = str(e).lower()
            if "already" in msg or "attached" in msg:
                self._shared_attached.add(node_id)
                return
            logger.warning(
                f"Failed to attach shared volume to {node_id}: {e}",
                exc_info=True,
            )

    def _find_stopped_for_reuse(self, user_node_type):
        """Return stopped Verda instances belonging to this cluster+user_node_type."""
        prefix = f"brr-{self.cluster_name}-"
        stopped_entries = {
            nid: tags
            for nid, tags in self._tags.all().items()
            if tags.get(TAG_RAY_CLUSTER_NAME) == self.cluster_name
            and tags.get(TAG_RAY_USER_NODE_TYPE) == user_node_type
        }
        if not stopped_entries:
            return []

        instances = self._sdk_call(self._client.instances.get)
        matches = []
        for inst in instances:
            inst_id = getattr(inst, "id", None)
            if inst_id not in stopped_entries:
                continue
            if not self._belongs_to_cluster(inst, prefix):
                continue
            if not self._is_stopped(getattr(inst, "status", "")):
                continue
            matches.append(inst)
        return matches

    def terminate_node(self, node_id):
        try:
            if self.cache_stopped_nodes:
                self._sdk_call(
                    self._client.instances.action,
                    node_id, action=self.stopped_node_action,
                )
                logger.info(
                    f"{self.stopped_node_action.title()}ed Verda instance {node_id}"
                )
            else:
                self._sdk_call(self._client.instances.action, node_id, action="delete")
                logger.info(f"Deleted Verda instance {node_id}")
                try:
                    self._tags.delete(node_id)
                except OSError:
                    pass
        finally:
            with self.lock:
                self._cache.pop(node_id, None)
                self._shared_attached.discard(node_id)

    def terminate_nodes(self, node_ids):
        for nid in node_ids:
            try:
                self.terminate_node(nid)
            except Exception:
                logger.warning(f"Failed to terminate Verda instance {nid}", exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_cached_or_fetch(self, node_id):
        with self.lock:
            cached = self._cache.get(node_id)
        if cached is not None:
            return cached
        return self._fetch_instance(node_id)

    def _fetch_instance(self, node_id):
        try:
            inst = self._sdk_call(self._client.instances.get_by_id, node_id)
        except Exception:
            logger.debug(f"Failed to fetch Verda instance {node_id}", exc_info=True)
            return None
        with self.lock:
            self._cache[node_id] = inst
        return inst

    def _wait_for_status(self, node_id, target_states, timeout=300, interval=5):
        """Poll instance status until it reaches one of target_states."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            inst = self._fetch_instance(node_id)
            state = (getattr(inst, "status", "") if inst else "").lower()
            if state in target_states:
                return inst
            if state in _TERMINAL_STATES:
                raise RuntimeError(
                    f"Verda instance {node_id} entered terminal state {state!r}"
                )
            time.sleep(interval)
        raise TimeoutError(
            f"Verda instance {node_id} did not reach {target_states} within {timeout}s"
        )

    @staticmethod
    def _is_active(state):
        return str(state).lower() in _RUNNING_STATES

    @staticmethod
    def _is_stopped(state):
        return str(state).lower() in _STOPPED_STATES

    @staticmethod
    def _is_terminal(state):
        return str(state).lower() in _TERMINAL_STATES
