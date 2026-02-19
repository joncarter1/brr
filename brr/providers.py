"""Provider abstraction for brr.

Each cloud provider (AWS, Nebius, etc.) implements the Provider interface.
Use get_provider(name) to get the provider instance.
"""


class Provider:
    """Base class for cloud providers. Subclass and implement all methods."""

    name: str

    def list_clusters(self, config):
        """Return list of cluster dicts.

        Each dict has: cluster_name, state, head_ip, instance_type, node_count, uptime.
        """
        raise NotImplementedError

    def find_head_ip(self, config, cluster_name):
        """Return the head node public IP for a running cluster, or None."""
        raise NotImplementedError

    def ssh_key(self, config):
        """Return the SSH private key path from config."""
        raise NotImplementedError

    def terminate_cluster(self, config, cluster_name):
        """Terminate all instances for a cluster. Return count terminated."""
        raise NotImplementedError

    def query_stopped(self, config, cluster_name=None):
        """Return stopped instances as list of dicts with instance_id and cluster_name."""
        raise NotImplementedError

    def terminate_by_ids(self, config, ids):
        """Terminate instances by ID list. Return count terminated."""
        raise NotImplementedError

    def bake_hint(self, config):
        """Return a bake status hint message, or None if nothing to say."""
        raise NotImplementedError


_PROVIDERS = {}


def register_provider(provider):
    """Register a provider instance by name."""
    _PROVIDERS[provider.name] = provider


def get_provider(name):
    """Get a provider by name, lazy-loading built-in providers on first access."""
    if name not in _PROVIDERS:
        if name == "aws":
            from brr.aws.provider import AWSProvider
            register_provider(AWSProvider())
        elif name == "nebius":
            from brr.nebius.provider import NebiusProvider
            register_provider(NebiusProvider())
        else:
            raise ValueError(f"Unknown provider: {name}")
    return _PROVIDERS[name]
