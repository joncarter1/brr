"""Custom Ray NodeProvider for Nebius AI Cloud.

Uses the Nebius Python SDK (gRPC) to manage compute instances.
Ray's autoscaler calls these methods to create/terminate/query nodes.
"""

import asyncio
import logging
import uuid
from threading import RLock

from ray.autoscaler.node_provider import NodeProvider

logger = logging.getLogger(__name__)


class NebiusNodeProvider(NodeProvider):
    """Ray NodeProvider backed by Nebius Compute API."""

    def __init__(self, provider_config, cluster_name):
        super().__init__(provider_config, cluster_name)
        self.lock = RLock()
        self.project_id = provider_config["project_id"]
        fs_id = provider_config.get("filesystem_id", "")
        self.filesystem_id = "" if not fs_id or "{{" in fs_id else fs_id

        from pathlib import Path
        from nebius.sdk import SDK

        creds_file = provider_config.get("credentials_file")
        if not creds_file:
            default_creds = Path.home() / ".nebius" / "credentials.json"
            if default_creds.exists():
                creds_file = str(default_creds)

        if creds_file:
            self._sdk = SDK(credentials_file_name=creds_file)
        else:
            self._sdk = SDK()

        # Persistent event loop — the SDK's internal asyncio primitives
        # (locks, events) bind to the first loop they run on. Using a single
        # loop avoids "bound to a different event loop" errors.
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._sdk.__aenter__())

        self._cache = {}  # node_id -> {tags, instance}

    def _run(self, coro):
        """Run an async coroutine on the provider's dedicated event loop.

        Thread-safe: Ray's updater may call from a different thread.
        """
        with self.lock:
            return self._loop.run_until_complete(coro)

    def _instance_client(self):
        from nebius.api.nebius.compute.v1 import InstanceServiceClient
        return InstanceServiceClient(self._sdk)

    def _disk_client(self):
        from nebius.api.nebius.compute.v1 import DiskServiceClient
        return DiskServiceClient(self._sdk)

    async def _find_disk_by_name(self, disk_client, name):
        """Find a disk by name. Returns disk ID or None."""
        from nebius.api.nebius.compute.v1 import ListDisksRequest

        page_token = None
        while True:
            req = ListDisksRequest(parent_id=self.project_id)
            if page_token:
                req.page_token = page_token
            resp = await disk_client.list(req)
            for disk in resp.items:
                if disk.metadata.name == name:
                    return disk.metadata.id
            if not resp.next_page_token:
                break
            page_token = resp.next_page_token
        return None

    # --- Node lifecycle ---

    async def _find_stopped_nodes(self, node_type):
        """Find stopped instances for this cluster and node type."""
        from nebius.api.nebius.compute.v1 import ListInstancesRequest

        client = self._instance_client()
        stopped = []
        page_token = None
        while True:
            req = ListInstancesRequest(parent_id=self.project_id)
            if page_token:
                req.page_token = page_token
            response = await client.list(req)
            for inst in response.items:
                labels = dict(inst.metadata.labels) if inst.metadata.labels else {}
                if labels.get("ray-cluster-name") != self.cluster_name:
                    continue
                if labels.get("ray-node-type") != node_type:
                    continue
                state = inst.status.state if inst.status else None
                if self._is_stopped(state):
                    stopped.append(inst.metadata.id)
            if not response.next_page_token:
                break
            page_token = response.next_page_token
        return stopped

    async def _start_instance(self, node_id):
        """Start a stopped instance."""
        from nebius.api.nebius.compute.v1 import StartInstanceRequest

        client = self._instance_client()
        operation = await client.start(StartInstanceRequest(id=node_id))
        await operation.wait()
        logger.info(f"Restarted stopped Nebius instance {node_id}")

    def create_node(self, node_config, tags, count):
        self._run(self._create_nodes(node_config, tags, count))

    async def _create_nodes(self, node_config, tags, count):
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.compute.v1 import (
            CreateDiskRequest,
            CreateInstanceRequest,
            DiskSpec,
            SourceImageFamily,
            InstanceSpec,
            InstanceRecoveryPolicy,
            ResourcesSpec,
            AttachedDiskSpec,
            ExistingDisk,
            AttachedFilesystemSpec,
            ExistingFilesystem,
            NetworkInterfaceSpec,
            IPAddress,
            PublicIPAddress,
        )

        # Reuse stopped instances before creating new ones
        node_type = tags.get("ray-node-type", "worker")
        remaining = count
        try:
            stopped = await self._find_stopped_nodes(node_type)
            for inst_id in stopped:
                if remaining <= 0:
                    break
                await self._start_instance(inst_id)
                await self._set_node_tags(inst_id, tags)
                inst = await self._fetch_instance(inst_id)
                if inst:
                    with self.lock:
                        self._cache[inst_id] = {"tags": dict(tags), "instance": inst}
                remaining -= 1
        except Exception:
            logger.warning("Failed to reuse stopped instances, creating new ones",
                           exc_info=True)

        instance_client = self._instance_client()
        disk_client = self._disk_client()

        for _ in range(remaining):
            node_type = tags.get("ray-node-type", "worker")
            name = f"{self.cluster_name}-{node_type}-{uuid.uuid4().hex[:8]}"

            labels = dict(tags)
            labels["ray-cluster-name"] = self.cluster_name

            # 1. Create or reuse boot disk
            disk_size_gb = node_config.get("disk_size_gb", 100)
            image_family = node_config.get("image_family", "ubuntu22.04-driverless")
            baked_image_id = node_config.get("baked_image_id")
            disk_name = f"{name}-boot"

            if baked_image_id:
                disk_spec = DiskSpec(
                    type=DiskSpec.DiskType.NETWORK_SSD,
                    source_image_id=baked_image_id,
                    size_gibibytes=disk_size_gb,
                )
            else:
                disk_spec = DiskSpec(
                    type=DiskSpec.DiskType.NETWORK_SSD,
                    source_image_family=SourceImageFamily(
                        image_family=image_family,
                    ),
                    size_gibibytes=disk_size_gb,
                )

            try:
                disk_op = await disk_client.create(CreateDiskRequest(
                    metadata=ResourceMetadata(
                        parent_id=self.project_id,
                        name=disk_name,
                        labels=labels,
                    ),
                    spec=disk_spec,
                ))
                await disk_op.wait()
                disk_id = disk_op.resource_id
                logger.info(f"Created boot disk {disk_id} for {name}")
            except Exception as e:
                if "ALREADY_EXISTS" not in str(e):
                    raise
                disk_id = await self._find_disk_by_name(disk_client, disk_name)
                if not disk_id:
                    raise
                logger.info(f"Reusing existing boot disk {disk_id} for {name}")

            # 2. Create instance with the disk attached
            # Inject SSH public key via cloud-init if available.
            # ssh_public_key may be the key content (starts with "ssh-")
            # or a file path.
            ssh_key = node_config.get("ssh_public_key")
            cloud_init = None
            if ssh_key:
                try:
                    if ssh_key.startswith("ssh-"):
                        pubkey = ssh_key
                    else:
                        with open(ssh_key) as f:
                            pubkey = f.read().strip()
                    cloud_init = f"#cloud-config\nssh_authorized_keys:\n  - {pubkey}\n"
                except FileNotFoundError:
                    logger.warning(f"SSH public key not found: {ssh_key}")

            # Attach shared filesystem if configured
            filesystems = []
            if self.filesystem_id:
                filesystems.append(
                    AttachedFilesystemSpec(
                        attach_mode=AttachedFilesystemSpec.AttachMode.READ_WRITE,
                        mount_tag="brr-shared",
                        existing_filesystem=ExistingFilesystem(id=self.filesystem_id),
                    )
                )

            spec = InstanceSpec(
                recovery_policy=InstanceRecoveryPolicy.FAIL,
                resources=ResourcesSpec(
                    platform=node_config["platform_id"],
                    preset=node_config["preset_id"],
                ),
                boot_disk=AttachedDiskSpec(
                    attach_mode=AttachedDiskSpec.AttachMode.READ_WRITE,
                    existing_disk=ExistingDisk(id=disk_id),
                ),
                filesystems=filesystems,
                network_interfaces=[
                    NetworkInterfaceSpec(
                        name="eth0",
                        subnet_id=node_config["subnet_id"],
                        ip_address=IPAddress(),
                        public_ip_address=PublicIPAddress(),
                    ),
                ],
                cloud_init_user_data=cloud_init,
            )

            op = await instance_client.create(CreateInstanceRequest(
                metadata=ResourceMetadata(
                    parent_id=self.project_id,
                    name=name,
                    labels=labels,
                ),
                spec=spec,
            ))
            await op.wait()
            node_id = op.resource_id
            logger.info(f"Created Nebius instance {node_id} ({name})")

            with self.lock:
                self._cache[node_id] = {"tags": dict(tags), "instance": None}
                # Fetch full instance to populate cache
            inst = await self._fetch_instance(node_id)
            if inst:
                with self.lock:
                    self._cache[node_id]["instance"] = inst

    def terminate_node(self, node_id):
        self._run(self._terminate_node(node_id))
        with self.lock:
            self._cache.pop(node_id, None)

    async def _terminate_node(self, node_id):
        from nebius.api.nebius.compute.v1 import StopInstanceRequest

        client = self._instance_client()
        operation = await client.stop(StopInstanceRequest(id=node_id))
        await operation.wait()
        logger.info(f"Stopped Nebius instance {node_id}")

    def terminate_nodes(self, node_ids):
        for node_id in node_ids:
            self.terminate_node(node_id)

    # --- Node queries ---

    def non_terminated_nodes(self, tag_filters):
        return self._run(self._non_terminated_nodes(tag_filters))

    async def _non_terminated_nodes(self, tag_filters):
        from nebius.api.nebius.compute.v1 import ListInstancesRequest

        client = self._instance_client()
        nodes = []
        page_token = None
        while True:
            req = ListInstancesRequest(parent_id=self.project_id)
            if page_token:
                req.page_token = page_token
            response = await client.list(req)

            for inst in response.items:
                labels = dict(inst.metadata.labels) if inst.metadata.labels else {}

                if labels.get("ray-cluster-name") != self.cluster_name:
                    continue
                if not all(labels.get(k) == v for k, v in tag_filters.items()):
                    continue

                state = inst.status.state if inst.status else None
                if self._is_terminal(state) or self._is_stopped(state):
                    continue

                node_id = inst.metadata.id
                with self.lock:
                    self._cache[node_id] = {"tags": labels, "instance": inst}
                nodes.append(node_id)

            if not response.next_page_token:
                break
            page_token = response.next_page_token

        return nodes

    def is_running(self, node_id):
        inst = self._get_cached_or_fetch(node_id)
        if not inst:
            return False
        state = inst.status.state if inst.status else None
        return self._is_active(state)

    def is_terminated(self, node_id):
        inst = self._get_cached_or_fetch(node_id)
        if not inst:
            return True
        state = inst.status.state if inst.status else None
        return self._is_terminal(state)

    # Nebius Instance state enum (protobuf integers):
    #   UNSPECIFIED=0, CREATING=1, UPDATING=2, STARTING=3,
    #   RUNNING=4, STOPPING=5, STOPPED=6, DELETING=7, ERROR=8
    _RUNNING_STATES = {4, "4", "RUNNING"}
    _STOPPED_STATES = {5, "5", 6, "6", "STOPPED", "STOPPING"}
    _TERMINAL_STATES = {7, "7", 8, "8", "ERROR", "DELETING"}

    @classmethod
    def _is_active(cls, state):
        return state in cls._RUNNING_STATES or "RUNNING" in str(state)

    @classmethod
    def _is_stopped(cls, state):
        return state in cls._STOPPED_STATES or any(
            s in str(state) for s in ("STOPPED", "STOPPING")
        )

    @classmethod
    def _is_terminal(cls, state):
        return state in cls._TERMINAL_STATES or any(
            s in str(state) for s in ("ERROR", "DELETING")
        )

    def node_tags(self, node_id):
        with self.lock:
            cached = self._cache.get(node_id)
        if cached:
            return dict(cached["tags"])
        inst = self._get_cached_or_fetch(node_id)
        if inst and inst.metadata.labels:
            return dict(inst.metadata.labels)
        return {}

    def set_node_tags(self, node_id, tags):
        with self.lock:
            cached = self._cache.get(node_id)
            if cached:
                cached["tags"].update(tags)
        self._run(self._set_node_tags(node_id, tags))

    async def _set_node_tags(self, node_id, tags):
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.compute.v1 import (
            GetInstanceRequest,
            UpdateInstanceRequest,
        )

        client = self._instance_client()
        instance = await client.get(GetInstanceRequest(id=node_id))

        labels = dict(instance.metadata.labels) if instance.metadata.labels else {}
        labels.update(tags)

        operation = await client.update(UpdateInstanceRequest(
            metadata=ResourceMetadata(
                id=node_id,
                parent_id=instance.metadata.parent_id,
                name=instance.metadata.name,
                labels=labels,
            ),
            spec=instance.spec,
        ))
        await operation.wait()

    def external_ip(self, node_id):
        inst = self._get_cached_or_fetch(node_id)
        return self._extract_ip(inst, public=True)

    def internal_ip(self, node_id):
        inst = self._get_cached_or_fetch(node_id)
        return self._extract_ip(inst, public=False)

    # --- Helpers ---

    def _get_cached_or_fetch(self, node_id):
        with self.lock:
            cached = self._cache.get(node_id)
        if cached and cached["instance"] is not None:
            return cached["instance"]
        return self._run(self._fetch_instance(node_id))

    async def _fetch_instance(self, node_id):
        from nebius.api.nebius.compute.v1 import GetInstanceRequest

        try:
            client = self._instance_client()
            instance = await client.get(GetInstanceRequest(id=node_id))
            with self.lock:
                if node_id not in self._cache:
                    self._cache[node_id] = {"tags": {}, "instance": instance}
                else:
                    self._cache[node_id]["instance"] = instance
            return instance
        except Exception:
            logger.debug(f"Failed to fetch instance {node_id}", exc_info=True)
            return None

    @staticmethod
    def _extract_ip(instance, public=True):
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
