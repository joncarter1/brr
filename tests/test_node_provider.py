"""Tests for the Nebius node provider's post-enumeration sweep contract.

Regression guards:

  - A transient Nebius INTERNAL during the disk-list/pagination phase of
    either post-enumeration sweep must NOT discard the already-built
    node list. If it did, Ray's autoscaler v2 monitor logs
    ``"No autoscaling state to report."`` and the autoscaler goes blind.
  - The recycle-disk TTL sweep must throttle (it would otherwise run a
    full disk-list pagination on every autoscaler poll, since
    ``_recycle_ttl_seconds`` defaults to 600 > 0).
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ray.autoscaler.tags import TAG_RAY_CLUSTER_NAME

from brr.nebius.node_provider import NebiusNodeProvider


def _list_response(items, next_token=""):
    return SimpleNamespace(items=items, next_page_token=next_token)


def _fake_instance(id_, cluster, state="RUNNING"):
    """Minimal stand-in for a Nebius protobuf Instance.

    Only the attributes _non_terminated_nodes actually reads are
    populated; everything else stays missing/empty so the test fails
    loudly if the real shape diverges.
    """
    return SimpleNamespace(
        metadata=SimpleNamespace(
            id=id_,
            name=id_,
            labels={TAG_RAY_CLUSTER_NAME: cluster},
            parent_id="proj-x",
            created_at=None,
        ),
        status=SimpleNamespace(state=state),
        spec=SimpleNamespace(
            boot_disk=SimpleNamespace(existing_disk=None),
            secondary_disks=[],
            preemptible=False,
        ),
    )


@pytest.fixture
def provider():
    """Build a NebiusNodeProvider against a mocked SDK.

    Patches ``nebius.sdk.SDK`` so ``__init__``'s ``self._sdk = SDK(...)``
    returns an SDK mock with awaitable ``__aenter__`` / ``__aexit__``.
    Disables the orphan-disk sweep so every observed ``disk_client.list``
    call comes from the recycle sweep — keeps assertions unambiguous.
    """
    fake_sdk = MagicMock(name="SDK_instance")
    fake_sdk.__aenter__ = AsyncMock(return_value=fake_sdk)
    fake_sdk.__aexit__ = AsyncMock(return_value=None)

    cfg = {
        "project_id": "proj-x",
        "orphan_disk_sweep_interval_seconds": 0,  # isolate the recycle sweep
    }

    with patch("nebius.sdk.SDK", return_value=fake_sdk):
        prov = NebiusNodeProvider(cfg, "my-cluster")

    # _is_terminal / _is_stopped classify Nebius state strings; for these
    # tests we only care about the sweep behaviour around a healthy node.
    prov._is_terminal = lambda s: False
    prov._is_stopped = lambda s: False

    yield prov

    # Tear down the background event-loop thread so the process exits
    # cleanly; null out _loop so __del__'s SDK __aexit__ short-circuits.
    prov._loop.call_soon_threadsafe(prov._loop.stop)
    prov._loop_thread.join(timeout=2)
    prov._loop.close()
    prov._loop = None


def test_non_terminated_nodes_survives_sweep_failure(provider, caplog):
    """A transient Nebius INTERNAL during disk-list pagination must not
    blind the autoscaler — the already-built node list must still be
    returned and the failure must be logged at WARNING."""
    inst = _fake_instance("inst-1", "my-cluster")

    instance_client = MagicMock()
    instance_client.list = AsyncMock(return_value=_list_response([inst]))
    disk_client = MagicMock()
    disk_client.list = AsyncMock(side_effect=Exception("INTERNAL: transient"))
    provider._instance_client = lambda: instance_client
    provider._disk_client = lambda: disk_client

    with caplog.at_level(logging.WARNING, logger="brr.nebius.node_provider"):
        nodes = provider.non_terminated_nodes({})

    assert nodes == ["inst-1"]
    assert any(
        "post-enumeration disk sweep failed" in r.message for r in caplog.records
    ), "expected the cleanup-guard warning to fire"


def test_recycle_sweep_is_throttled(provider):
    """Two back-to-back autoscaler polls must not each fire a full
    disk-list pagination — the recycle sweep is gated by
    ``_recycle_sweep_interval`` (default 120s)."""
    inst = _fake_instance("inst-1", "my-cluster")

    instance_client = MagicMock()
    instance_client.list = AsyncMock(return_value=_list_response([inst]))
    disk_client = MagicMock()
    disk_client.list = AsyncMock(return_value=_list_response([]))
    provider._instance_client = lambda: instance_client
    provider._disk_client = lambda: disk_client

    provider.non_terminated_nodes({})
    provider.non_terminated_nodes({})

    # Orphan sweep is disabled in the fixture, so every disk.list() comes
    # from the recycle sweep. With the throttle the second poll is gated
    # out; without it the count would be 2.
    assert disk_client.list.call_count == 1
