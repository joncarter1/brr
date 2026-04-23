"""Nebius provider implementation for brr.

Multi-region aware: iterates over all configured regions for list operations
and resolves each cluster to its region via staged brr_meta.json (with
multi-region search as a fallback for legacy clusters).
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from brr.providers import Provider
from brr.state import (
    nebius_regions,
    nebius_region_config,
    staging_dir_for,
)


def _region_project_id(config, region):
    return nebius_region_config(config, region).get("NEBIUS_PROJECT_ID", "")


def _read_cluster_region(cluster_name):
    """Read the region recorded in the cluster's staging brr_meta.json, or None."""
    meta_path = staging_dir_for(cluster_name, "nebius") / "brr_meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text()).get("region")
    except (json.JSONDecodeError, OSError):
        return None


def _resolve_cluster_region(config, cluster_name):
    """Find which configured region contains the given cluster.

    Prefers brr_meta.json for O(1) lookup. Falls back to parallel
    query_clusters across all regions (legacy clusters with no meta).
    Returns None if the cluster isn't found anywhere.
    """
    from brr.nebius.nodes import query_clusters

    recorded = _read_cluster_region(cluster_name)
    regions = nebius_regions(config)
    if recorded and recorded in regions:
        return recorded

    with ThreadPoolExecutor(max_workers=max(1, len(regions))) as pool:
        futures = {
            pool.submit(query_clusters, _region_project_id(config, r)): r
            for r in regions
        }
        for fut in as_completed(futures):
            region = futures[fut]
            try:
                clusters = fut.result()
            except Exception:
                continue
            for c in clusters:
                if c.get("cluster_name") == cluster_name:
                    return region
    return None


class NebiusProvider(Provider):

    name = "nebius"

    def list_clusters(self, config):
        """Aggregate clusters across every configured region in parallel."""
        from brr.nebius.nodes import query_clusters

        regions = nebius_regions(config)
        all_clusters = []
        with ThreadPoolExecutor(max_workers=max(1, len(regions))) as pool:
            futures = {
                pool.submit(query_clusters, _region_project_id(config, r)): r
                for r in regions
            }
            for fut in as_completed(futures):
                region = futures[fut]
                try:
                    clusters = fut.result()
                except Exception:
                    continue
                for c in clusters:
                    c["region"] = region
                    all_clusters.append(c)
        return all_clusters

    def find_head_ip(self, config, cluster_name):
        from brr.nebius.nodes import query_head_ip
        region = _resolve_cluster_region(config, cluster_name)
        if not region:
            return None
        return query_head_ip(_region_project_id(config, region), cluster_name)

    def ssh_key(self, config):
        return config.get("NEBIUS_SSH_KEY", "")

    def terminate_cluster(self, config, cluster_name):
        from brr.nebius.nodes import terminate_cluster_instances
        region = _resolve_cluster_region(config, cluster_name)
        if not region:
            return 0
        return terminate_cluster_instances(_region_project_id(config, region), cluster_name)

    def query_stopped(self, config, cluster_name=None):
        """Return stopped instances. If cluster_name is given, scope to its region;
        otherwise aggregate across every configured region."""
        from brr.nebius.nodes import query_stopped_instances

        regions = nebius_regions(config)
        if cluster_name:
            region = _resolve_cluster_region(config, cluster_name)
            if not region:
                return []
            return query_stopped_instances(_region_project_id(config, region), cluster_name)

        # Fan out across all regions.
        results = []
        with ThreadPoolExecutor(max_workers=max(1, len(regions))) as pool:
            futures = [
                pool.submit(query_stopped_instances, _region_project_id(config, r), None)
                for r in regions
            ]
            for fut in as_completed(futures):
                try:
                    results.extend(fut.result())
                except Exception:
                    continue
        return results

    def terminate_by_ids(self, config, ids):
        """Terminate Nebius instances by ID.

        Instance IDs are globally unique in Nebius and the delete API doesn't
        require a project_id, so one call handles instances from any region.
        """
        from brr.nebius.nodes import terminate_instances
        if not ids:
            return 0
        regions = nebius_regions(config)
        pid = _region_project_id(config, regions[0]) if regions else ""
        return terminate_instances(pid, ids)
