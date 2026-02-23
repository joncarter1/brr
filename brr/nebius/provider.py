"""Nebius provider implementation for brr."""

from brr.providers import Provider


class NebiusProvider(Provider):

    name = "nebius"

    def list_clusters(self, config):
        from brr.nebius.nodes import query_clusters
        return query_clusters(config.get("NEBIUS_PROJECT_ID", ""))

    def find_head_ip(self, config, cluster_name):
        from brr.nebius.nodes import query_head_ip
        return query_head_ip(config.get("NEBIUS_PROJECT_ID", ""), cluster_name)

    def ssh_key(self, config):
        return config.get("NEBIUS_SSH_KEY", "")

    def terminate_cluster(self, config, cluster_name):
        from brr.nebius.nodes import terminate_cluster_instances
        return terminate_cluster_instances(config.get("NEBIUS_PROJECT_ID", ""), cluster_name)

    def query_stopped(self, config, cluster_name=None):
        from brr.nebius.nodes import query_stopped_instances
        return query_stopped_instances(config.get("NEBIUS_PROJECT_ID", ""), cluster_name)

    def terminate_by_ids(self, config, ids):
        from brr.nebius.nodes import terminate_instances
        return terminate_instances(config.get("NEBIUS_PROJECT_ID", ""), ids)

    def bake_hint(self, config):
        from brr.templates import global_setup_hash
        bake_hash = config.get("NEBIUS_BAKE_SETUP_HASH", "")
        has_baked = config.get("NEBIUS_IMAGE_CPU_BAKED") or config.get("NEBIUS_IMAGE_GPU_BAKED")
        if has_baked and bake_hash and bake_hash != global_setup_hash():
            return (
                "Warning: setup.sh has changed since last bake. "
                "Run `brr bake nebius` to rebuild."
            )
        elif not has_baked:
            return "Tip: Run `brr bake nebius` to pre-bake setup into images for faster boot."
        return None
