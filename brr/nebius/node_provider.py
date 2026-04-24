"""Custom Ray NodeProvider for Nebius AI Cloud.

Uses the Nebius Python SDK (gRPC) to manage compute instances.
Ray's autoscaler calls these methods to create/terminate/query nodes.
"""

import asyncio
import logging
import threading
import uuid

from ray.autoscaler.node_provider import NodeProvider
from ray.autoscaler.tags import TAG_RAY_CLUSTER_NAME, TAG_RAY_USER_NODE_TYPE

logger = logging.getLogger(__name__)


class NebiusNodeProvider(NodeProvider):
    """Ray NodeProvider backed by Nebius Compute API."""

    def __init__(self, provider_config, cluster_name):
        super().__init__(provider_config, cluster_name)
        self.lock = threading.RLock()  # guards self._cache only
        self.project_id = provider_config["project_id"]
        fs_id = provider_config.get("filesystem_id", "")
        self.filesystem_id = "" if not fs_id or "{{" in fs_id else fs_id
        sa_id = provider_config.get("service_account_id", "")
        self.service_account_id = "" if not sa_id or "{{" in sa_id else sa_id
        sg_id = provider_config.get("security_group_id", "")
        self.security_group_id = "" if not sg_id or "{{" in sg_id else sg_id
        # Unlike AWS, stopped Nebius instances still incur disk costs.
        # Default to deleting nodes on scale-down to avoid surprise charges.
        # Set cache_stopped_nodes: true in the provider config to keep them.
        self.cache_stopped_nodes = provider_config.get("cache_stopped_nodes", False)
        # Preempted preemptible instances can preserve their boot disk for a
        # window so that a fresh replacement spawned by pending demand skips
        # setup.sh. 0 disables the feature. Default ~10 min: enough for Ray
        # to notice NodeTerminated and redrive create_node; short enough that
        # disks don't linger under sustained low demand.
        self._recycle_ttl_seconds = int(provider_config.get("preempt_recycle_ttl_seconds", 600))

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

        # Persistent event loop on a dedicated thread. The SDK's asyncio
        # primitives bind to the first loop they see, so we keep exactly one
        # loop — but we run it continuously in the background so coroutines
        # submitted from different threads interleave at await points rather
        # than serializing end-to-end behind run_until_complete + a lock.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="nebius-provider-loop",
            daemon=True,
        )
        self._loop_thread.start()
        # Bring the SDK up on the loop before accepting work.
        asyncio.run_coroutine_threadsafe(self._sdk.__aenter__(), self._loop).result()

        self._cache = {}  # node_id -> {tags, instance}
        # Instances currently being created (by name) or set up post-restart
        # (by id). Hidden from non_terminated_nodes so Ray's updater doesn't
        # race our in-flight Nebius operation — any UpdateInstance while a
        # create/start op is active returns FAILED_PRECONDITION, which Ray
        # surfaces as "launch failed" and terminates the node.
        self._hidden_names = set()
        self._hidden_ids = set()
        # Orphan-stopped instances currently being deleted by the background
        # sweep in non_terminated_nodes. Guards against re-submitting the same
        # delete on subsequent autoscaler polls while the first is in flight.
        self._orphan_deleting = set()
        # Instances whose boot disk is being preserved for recycling
        # (DeleteInstance in flight, don't re-submit).
        self._preserving_disk_for = set()
        # Recyclable disks being deleted by the TTL sweep.
        self._orphan_disk_deleting = set()
        # Recyclable disks currently being claimed (label-strip in flight) so
        # two concurrent create_node calls don't grab the same disk.
        self._claiming_disks = set()

    def _run(self, coro):
        """Submit a coroutine to the provider's background event loop.

        Thread-safe: any thread may call this; coroutines interleave on the
        loop at await points instead of blocking every other provider call.
        """
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def __del__(self):
        loop = getattr(self, "_loop", None)
        if loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._sdk.__aexit__(None, None, None), loop
            )
            fut.result(timeout=10)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
            self._loop_thread.join(timeout=5)
        except Exception:
            pass

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
                if labels.get(TAG_RAY_CLUSTER_NAME) != self.cluster_name:
                    continue
                if labels.get(TAG_RAY_USER_NODE_TYPE) != node_type:
                    continue
                state = inst.status.state if inst.status else None
                if self._is_stopped(state):
                    stopped.append(inst.metadata.id)
            if not response.next_page_token:
                break
            page_token = response.next_page_token
        return stopped

    async def _ensure_security_group(self, node_id):
        """Ensure the instance's network interface has our security group."""
        from nebius.api.nebius.compute.v1 import (
            GetInstanceRequest,
            UpdateInstanceRequest,
            SecurityGroup,
        )
        from nebius.api.nebius.common.v1 import ResourceMetadata

        client = self._instance_client()
        instance = await client.get(GetInstanceRequest(id=node_id))

        iface = instance.spec.network_interfaces[0]
        existing_sg_ids = {sg.id for sg in iface.security_groups}
        if self.security_group_id in existing_sg_ids:
            return

        iface.security_groups.append(SecurityGroup(id=self.security_group_id))
        operation = await client.update(UpdateInstanceRequest(
            metadata=ResourceMetadata(
                id=node_id,
                parent_id=instance.metadata.parent_id,
                name=instance.metadata.name,
                labels=dict(instance.metadata.labels) if instance.metadata.labels else {},
            ),
            spec=instance.spec,
        ))
        await operation.wait()
        logger.info(f"Attached security group to instance {node_id}")

    async def _start_instance(self, node_id):
        """Start a stopped instance."""
        from nebius.api.nebius.compute.v1 import StartInstanceRequest

        client = self._instance_client()
        operation = await client.start(StartInstanceRequest(id=node_id))
        await operation.wait()
        logger.info(f"Restarted stopped Nebius instance {node_id}")

    _KNOWN_NODE_CONFIG_KEYS = {
        "platform_id", "preset_id", "image_family",
        "subnet_id", "disk_size_gb", "disk_type",
        "preemptible", "ssh_public_key",
        "gpu_cluster_id",
    }

    def create_node(self, node_config, tags, count):
        unknown = set(node_config) - self._KNOWN_NODE_CONFIG_KEYS
        if unknown:
            raise ValueError(
                f"Unknown node_config keys (typo?): {', '.join(sorted(unknown))}"
            )
        self._run(self._create_nodes(node_config, tags, count))

    async def _create_nodes(self, node_config, tags, count):
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.compute.v1 import (
            CreateDiskRequest,
            CreateInstanceRequest,
            DiskSpec,
            SourceImageFamily,
            InstanceSpec,
            InstanceGpuClusterSpec,
            InstanceRecoveryPolicy,
            PreemptibleSpec,
            ResourcesSpec,
            AttachedDiskSpec,
            ExistingDisk,
            AttachedFilesystemSpec,
            ExistingFilesystem,
            NetworkInterfaceSpec,
            IPAddress,
            PublicIPAddress,
            SecurityGroup,
        )

        # Reuse stopped nodes — but not preemptible ones, which may have been
        # preempted due to capacity and are unlikely to restart successfully.
        # Match on the user node type (e.g. "ray.worker.h200d"), NOT the kind
        # ("head"/"worker") — otherwise every worker type collapses together
        # and we risk restarting an h200x8s as an h200d.
        node_type = tags.get(TAG_RAY_USER_NODE_TYPE)
        remaining = count
        excess_stopped = []
        is_preemptible = bool(node_config.get("preemptible"))
        try:
            stopped = await self._find_stopped_nodes(node_type) if node_type else []
            if is_preemptible:
                # Don't attempt restart — preempted instances likely lack capacity,
                # and operation.wait() could block for a long time.
                if stopped:
                    logger.info(f"Skipping {len(stopped)} stopped preemptible instance(s), creating new")
                excess_stopped = stopped
            else:
                if stopped:
                    logger.info(f"Found {len(stopped)} stopped {node_type} instance(s)")
                restarted = 0
                for inst_id in stopped:
                    if remaining <= 0:
                        break
                    with self.lock:
                        self._hidden_ids.add(inst_id)
                    try:
                        # Ensure security group is attached before starting
                        # (may be missing on nodes created before SG feature)
                        if self.security_group_id:
                            await self._ensure_security_group(inst_id)
                        await self._start_instance(inst_id)
                        await self._set_node_tags(inst_id, tags)
                        inst = await self._fetch_instance(inst_id)
                        if inst:
                            with self.lock:
                                self._cache[inst_id] = {"tags": dict(tags), "instance": inst}
                    finally:
                        with self.lock:
                            self._hidden_ids.discard(inst_id)
                    remaining -= 1
                    restarted += 1
                excess_stopped = stopped[restarted:]
        except Exception:
            logger.warning("Failed to reuse stopped instances, creating new ones",
                           exc_info=True)

        # Clean up orphaned stopped nodes when not caching (or always for preemptible)
        if (is_preemptible or not self.cache_stopped_nodes) and excess_stopped:
            logger.info(f"Cleaning up {len(excess_stopped)} excess stopped instance(s)")
            for inst_id in excess_stopped:
                try:
                    await self._delete_instance_and_disk(inst_id)
                except Exception:
                    logger.warning(f"Failed to clean up instance {inst_id}", exc_info=True)

        instance_client = self._instance_client()
        disk_client = self._disk_client()

        for _ in range(remaining):
            # Nebius resource names can't contain dots, so sanitize
            # "ray.worker.h200d" -> "ray-worker-h200d".
            raw_type = tags.get(TAG_RAY_USER_NODE_TYPE) or tags.get("ray-node-type", "worker")
            sanitized = raw_type.replace(".", "-")
            name = f"{self.cluster_name}-{sanitized}-{uuid.uuid4().hex[:8]}"

            labels = dict(tags)
            labels[TAG_RAY_CLUSTER_NAME] = self.cluster_name

            # 1. Create or reuse boot disk
            disk_size_gb = node_config.get("disk_size_gb", 100)
            image_family = node_config.get("image_family", "ubuntu22.04-driverless")
            disk_name = f"{name}-boot"

            # Map disk_type string to DiskSpec enum
            _DISK_TYPES = {
                "network-ssd": DiskSpec.DiskType.NETWORK_SSD,
                "network-ssd-nonreplicated": DiskSpec.DiskType.NETWORK_SSD_NON_REPLICATED,
                "network-ssd-io-m3": DiskSpec.DiskType.NETWORK_SSD_IO_M3,
            }
            disk_type = _DISK_TYPES.get(
                node_config.get("disk_type", "network-ssd"),
                DiskSpec.DiskType.NETWORK_SSD,
            )

            disk_spec = DiskSpec(
                type=disk_type,
                source_image_family=SourceImageFamily(
                    image_family=image_family,
                ),
                size_gibibytes=disk_size_gb,
            )

            # Try recycling a preserved disk from a recent preemption. Gated
            # on matching user_node_type + size + image so template changes
            # force a fresh disk. Claim via label-strip before attach so two
            # concurrent create_node calls can't grab the same disk.
            disk_id = None
            recycled = False
            user_node_type = tags.get(TAG_RAY_USER_NODE_TYPE)
            if user_node_type and self._recycle_ttl_seconds > 0:
                candidate = await self._find_recyclable_disk(
                    user_node_type, disk_size_gb, image_family,
                )
                if candidate:
                    with self.lock:
                        self._claiming_disks.add(candidate)
                    try:
                        if await self._claim_disk(candidate):
                            disk_id = candidate
                            recycled = True
                            logger.info(
                                f"Recycled boot disk {disk_id} for {name} "
                                f"(skipping setup.sh reinstall)"
                            )
                    finally:
                        with self.lock:
                            self._claiming_disks.discard(candidate)

            if not recycled:
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

            spec_kwargs = dict(
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
                        security_groups=[SecurityGroup(id=self.security_group_id)] if self.security_group_id else [],
                    ),
                ],
                cloud_init_user_data=cloud_init,
            )
            if self.service_account_id:
                spec_kwargs["service_account_id"] = self.service_account_id
            preemptible = node_config.get("preemptible")
            if preemptible:
                priority = preemptible if isinstance(preemptible, int) else 1
                spec_kwargs["preemptible"] = PreemptibleSpec(
                    on_preemption=PreemptibleSpec.PreemptionPolicy.STOP,
                    priority=priority,
                )
            gpu_cluster_id = node_config.get("gpu_cluster_id")
            if gpu_cluster_id and "{{" not in str(gpu_cluster_id):
                spec_kwargs["gpu_cluster"] = InstanceGpuClusterSpec(id=gpu_cluster_id)
            spec = InstanceSpec(**spec_kwargs)

            with self.lock:
                self._hidden_names.add(name)
            try:
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
            finally:
                with self.lock:
                    self._hidden_names.discard(name)

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
        if self.cache_stopped_nodes:
            from nebius.api.nebius.compute.v1 import StopInstanceRequest

            client = self._instance_client()
            operation = await client.stop(StopInstanceRequest(id=node_id))
            await operation.wait()
            logger.info(f"Stopped Nebius instance {node_id}")
        else:
            await self._delete_instance_and_disk(node_id)

    async def _delete_instance_and_disk(self, node_id):
        """Delete an instance and its boot disk."""
        from nebius.api.nebius.compute.v1 import (
            DeleteInstanceRequest,
            DeleteDiskRequest,
        )

        # Fetch instance to find boot disk before deletion
        inst = await self._fetch_instance(node_id)
        boot_disk_id = None
        if inst and inst.spec and inst.spec.boot_disk:
            boot_disk_id = inst.spec.boot_disk.existing_disk.id

        client = self._instance_client()
        operation = await client.delete(DeleteInstanceRequest(id=node_id))
        await operation.wait()
        logger.info(f"Deleted Nebius instance {node_id}")

        # Clean up boot disk
        if boot_disk_id:
            try:
                disk_client = self._disk_client()
                disk_op = await disk_client.delete(DeleteDiskRequest(id=boot_disk_id))
                await disk_op.wait()
                logger.info(f"Deleted boot disk {boot_disk_id}")
            except Exception:
                logger.warning(f"Failed to delete boot disk {boot_disk_id}", exc_info=True)

    async def _delete_orphan(self, node_id):
        """Delete a stopped instance that the autoscaler never asked for."""
        try:
            logger.info(f"Deleting orphan-stopped Nebius instance {node_id}")
            await self._delete_instance_and_disk(node_id)
        except Exception:
            logger.warning(
                f"Failed to delete orphan-stopped instance {node_id}",
                exc_info=True,
            )
        finally:
            with self.lock:
                self._orphan_deleting.discard(node_id)

    # --- Boot-disk recycling across preemption ---

    _RECYCLE_LABEL_PREFIX = "brr-recycle-"
    _RECYCLE_UNTIL_LABEL = "brr-recycle-until"
    _RECYCLE_SIZE_LABEL = "brr-recycle-size-gb"
    _RECYCLE_IMAGE_LABEL = "brr-recycle-image-family"

    async def _preserve_disk_delete_instance(self, node_id):
        """Delete a preempted instance but keep its boot disk tagged for reuse.

        The disk gets recycle labels (TTL + size + image) so later create_node
        calls can find it. If anything in the preservation path fails, fall
        back to the full delete so we don't leak the instance.
        """
        import time
        from nebius.api.nebius.compute.v1 import UpdateDiskRequest
        from nebius.api.nebius.common.v1 import ResourceMetadata

        try:
            inst = await self._fetch_instance(node_id)
            if not inst or not (inst.spec and inst.spec.boot_disk):
                raise RuntimeError("instance has no boot disk to preserve")
            boot_disk_id = inst.spec.boot_disk.existing_disk.id
            disk_client = self._disk_client()

            # Read disk metadata BEFORE deleting the instance so we can carry
            # size + image_family into the recycle labels. (source_image_family
            # is set at CreateDisk time only.)
            from nebius.api.nebius.compute.v1 import GetDiskRequest, DeleteInstanceRequest
            disk = await disk_client.get(GetDiskRequest(id=boot_disk_id))
            existing_labels = dict(disk.metadata.labels) if disk.metadata.labels else {}
            existing_size = int(disk.spec.size_gibibytes) if disk.spec and disk.spec.size_gibibytes else 0
            image_family = ""
            if disk.spec and disk.spec.source_image_family:
                image_family = disk.spec.source_image_family.image_family or ""

            # Delete the instance FIRST so the disk detaches — UpdateDisk on an
            # attached disk can be rejected for "active operation" reasons.
            client = self._instance_client()
            del_op = await client.delete(DeleteInstanceRequest(id=node_id))
            await del_op.wait()

            # Now label the orphan disk for recycling.
            expiry = int(time.time()) + self._recycle_ttl_seconds
            new_labels = dict(existing_labels)
            new_labels[self._RECYCLE_UNTIL_LABEL] = str(expiry)
            new_labels[self._RECYCLE_SIZE_LABEL] = str(existing_size)
            new_labels[self._RECYCLE_IMAGE_LABEL] = image_family
            update_req = UpdateDiskRequest(
                metadata=ResourceMetadata(id=boot_disk_id, labels=new_labels),
            )
            op = await disk_client.update(update_req)
            await op.wait()
            logger.info(
                f"Preserved boot disk {boot_disk_id} (TTL {self._recycle_ttl_seconds}s), "
                f"deleted preempted instance {node_id}"
            )
        except Exception:
            logger.warning(
                f"Preserve-disk failed for preempted {node_id}; falling back to full delete",
                exc_info=True,
            )
            try:
                await self._delete_instance_and_disk(node_id)
            except Exception:
                logger.warning(f"Fallback delete also failed for {node_id}", exc_info=True)
        finally:
            with self.lock:
                self._preserving_disk_for.discard(node_id)

    async def _find_recyclable_disk(self, user_node_type, disk_size_gb, image_family):
        """Return a recyclable disk id matching the node_type/size/image, or None."""
        import time
        from nebius.api.nebius.compute.v1 import ListDisksRequest

        if self._recycle_ttl_seconds <= 0:
            return None

        client = self._disk_client()
        page_token = None
        candidates = []
        now = int(time.time())
        with self.lock:
            claiming = set(self._claiming_disks)
            deleting = set(self._orphan_disk_deleting)
        while True:
            req = ListDisksRequest(parent_id=self.project_id)
            if page_token:
                req.page_token = page_token
            resp = await client.list(req)
            for d in resp.items:
                if d.metadata.id in claiming or d.metadata.id in deleting:
                    continue
                labels = dict(d.metadata.labels) if d.metadata.labels else {}
                if labels.get(TAG_RAY_CLUSTER_NAME) != self.cluster_name:
                    continue
                if labels.get(TAG_RAY_USER_NODE_TYPE) != user_node_type:
                    continue
                until = labels.get(self._RECYCLE_UNTIL_LABEL)
                if not until:
                    continue
                try:
                    until_ts = int(until)
                except ValueError:
                    continue
                if until_ts <= now:
                    continue  # expired — leave for the sweep to delete
                # Gate on size + image match so template changes don't reuse
                # the wrong disk.
                if labels.get(self._RECYCLE_SIZE_LABEL) != str(disk_size_gb):
                    continue
                if labels.get(self._RECYCLE_IMAGE_LABEL) != image_family:
                    continue
                created_ts = 0
                if d.metadata.created_at:
                    created_ts = d.metadata.created_at.seconds
                candidates.append((created_ts, d.metadata.id))
            if not resp.next_page_token:
                break
            page_token = resp.next_page_token

        if not candidates:
            return None
        # FIFO: oldest first, so the youngest disks stay around for other claimants.
        candidates.sort()
        return candidates[0][1]

    async def _claim_disk(self, disk_id):
        """Strip recycle labels from a disk. Returns True on success.

        False means the disk disappeared / was claimed by someone else / got
        swept; the caller should fall back to creating a fresh disk.
        """
        from nebius.api.nebius.compute.v1 import GetDiskRequest, UpdateDiskRequest
        from nebius.api.nebius.common.v1 import ResourceMetadata

        client = self._disk_client()
        try:
            disk = await client.get(GetDiskRequest(id=disk_id))
        except Exception:
            return False
        labels = dict(disk.metadata.labels) if disk.metadata.labels else {}
        # Someone else already claimed it.
        if self._RECYCLE_UNTIL_LABEL not in labels:
            return False
        for key in (self._RECYCLE_UNTIL_LABEL, self._RECYCLE_SIZE_LABEL, self._RECYCLE_IMAGE_LABEL):
            labels.pop(key, None)
        try:
            op = await client.update(UpdateDiskRequest(
                metadata=ResourceMetadata(id=disk_id, labels=labels),
            ))
            await op.wait()
            return True
        except Exception:
            logger.warning(f"Failed to claim recycle disk {disk_id}", exc_info=True)
            return False

    async def _sweep_expired_recycle_disks(self):
        """Delete recycle disks whose TTL has passed."""
        import time
        from nebius.api.nebius.compute.v1 import ListDisksRequest

        client = self._disk_client()
        now = int(time.time())
        page_token = None
        to_delete = []
        while True:
            req = ListDisksRequest(parent_id=self.project_id)
            if page_token:
                req.page_token = page_token
            resp = await client.list(req)
            for d in resp.items:
                labels = dict(d.metadata.labels) if d.metadata.labels else {}
                if labels.get(TAG_RAY_CLUSTER_NAME) != self.cluster_name:
                    continue
                until = labels.get(self._RECYCLE_UNTIL_LABEL)
                if not until:
                    continue
                try:
                    until_ts = int(until)
                except ValueError:
                    continue
                if until_ts <= now:
                    to_delete.append(d.metadata.id)
            if not resp.next_page_token:
                break
            page_token = resp.next_page_token

        for disk_id in to_delete:
            should_submit = False
            with self.lock:
                if disk_id not in self._orphan_disk_deleting:
                    self._orphan_disk_deleting.add(disk_id)
                    should_submit = True
            if should_submit:
                asyncio.run_coroutine_threadsafe(
                    self._delete_expired_recycle_disk(disk_id), self._loop
                )

    async def _delete_expired_recycle_disk(self, disk_id):
        from nebius.api.nebius.compute.v1 import DeleteDiskRequest
        try:
            client = self._disk_client()
            op = await client.delete(DeleteDiskRequest(id=disk_id))
            await op.wait()
            logger.info(f"Deleted expired recycle disk {disk_id}")
        except Exception:
            logger.warning(f"Failed to delete expired recycle disk {disk_id}", exc_info=True)
        finally:
            with self.lock:
                self._orphan_disk_deleting.discard(disk_id)

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
        with self.lock:
            hidden_names = set(self._hidden_names)
            hidden_ids = set(self._hidden_ids)
        while True:
            req = ListInstancesRequest(parent_id=self.project_id)
            if page_token:
                req.page_token = page_token
            response = await client.list(req)

            for inst in response.items:
                if inst.metadata.id in hidden_ids:
                    continue
                if inst.metadata.name in hidden_names:
                    continue
                labels = dict(inst.metadata.labels) if inst.metadata.labels else {}

                if labels.get(TAG_RAY_CLUSTER_NAME) != self.cluster_name:
                    continue
                if not all(labels.get(k) == v for k, v in tag_filters.items()):
                    continue

                state = inst.status.state if inst.status else None
                if self._is_terminal(state):
                    continue
                if self._is_stopped(state):
                    # When cache_stopped_nodes is False, stopped instances are
                    # orphans — preempted by Nebius, stopped by idle-shutdown,
                    # or stopped from the console. Ray's scale-down deletes
                    # directly, so anything we see stopped here wasn't asked
                    # for. Fire-and-forget a delete so the poll loop stays
                    # fast; tracking sets prevent double-submits across polls.
                    if not self.cache_stopped_nodes and inst.metadata.id not in hidden_ids:
                        # Preempted preemptible instance + recycling enabled:
                        # preserve the boot disk so a fresh replacement can
                        # reattach it and skip setup.sh.
                        is_preempt = bool(inst.spec and inst.spec.preemptible)
                        recycle = is_preempt and self._recycle_ttl_seconds > 0
                        if recycle:
                            should_submit = False
                            with self.lock:
                                if inst.metadata.id not in self._preserving_disk_for:
                                    self._preserving_disk_for.add(inst.metadata.id)
                                    should_submit = True
                            if should_submit:
                                asyncio.run_coroutine_threadsafe(
                                    self._preserve_disk_delete_instance(inst.metadata.id),
                                    self._loop,
                                )
                        else:
                            should_submit = False
                            with self.lock:
                                if inst.metadata.id not in self._orphan_deleting:
                                    self._orphan_deleting.add(inst.metadata.id)
                                    should_submit = True
                            if should_submit:
                                asyncio.run_coroutine_threadsafe(
                                    self._delete_orphan(inst.metadata.id), self._loop
                                )
                    continue

                node_id = inst.metadata.id
                with self.lock:
                    self._cache[node_id] = {"tags": labels, "instance": inst}
                nodes.append(node_id)

            if not response.next_page_token:
                break
            page_token = response.next_page_token

        # Expire recycle disks that weren't reclaimed within the TTL.
        if self._recycle_ttl_seconds > 0:
            await self._sweep_expired_recycle_disks()

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
        # Defense in depth: retry on FAILED_PRECONDITION ("cannot update
        # instance with active operation"). Ray's updater may race a
        # create/start operation that's still settling; without this the
        # exception propagates up and Ray terminates the node.
        total_slept = 0.0
        while True:
            try:
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
                return
            except Exception as e:
                msg = str(e)
                if ("FAILED_PRECONDITION" in msg
                        and "active operation" in msg
                        and total_slept < 600):
                    delay = min(5.0, 1.0 + total_slept / 30)
                    await asyncio.sleep(delay)
                    total_slept += delay
                    continue
                raise

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
