"""Verda provider implementation for brr."""

from brr.providers import Provider


class VerdaProvider(Provider):

    name = "verda"

    def list_clusters(self, config):
        from brr.verda.nodes import query_clusters
        return query_clusters()

    def find_head_ip(self, config, cluster_name):
        from brr.verda.nodes import query_head_ip
        return query_head_ip(cluster_name)

    def ssh_key(self, config):
        return config.get("VERDA_SSH_KEY", "")

    def ssh_user(self, config) -> str:
        return "root"

    def terminate_cluster(self, config, cluster_name):
        from brr.verda.nodes import terminate_cluster_instances
        return terminate_cluster_instances(cluster_name)

    def query_stopped(self, config, cluster_name=None):
        from brr.verda.nodes import query_stopped_instances
        return query_stopped_instances(cluster_name)

    def terminate_by_ids(self, config, ids):
        from brr.verda.nodes import terminate_instances
        return terminate_instances(ids)
