"""AWS provider implementation for brr."""

from brr.providers import Provider


class AWSProvider(Provider):

    name = "aws"

    def list_clusters(self, config):
        from brr.aws.nodes import query_ray_clusters
        return query_ray_clusters(config.get("AWS_REGION", "us-east-1"))

    def find_head_ip(self, config, cluster_name):
        clusters = self.list_clusters(config)
        match = next(
            (c for c in clusters if c["cluster_name"] == cluster_name and c["state"] == "running"),
            None,
        )
        if match and match["head_ip"] != "-":
            return match["head_ip"]
        return None

    def ssh_key(self, config):
        return config.get("AWS_SSH_KEY", "")

    def terminate_cluster(self, config, cluster_name):
        from brr.aws.nodes import terminate_cluster_instances
        return terminate_cluster_instances(config.get("AWS_REGION", "us-east-1"), cluster_name)

    def query_stopped(self, config, cluster_name=None):
        import boto3
        region = config.get("AWS_REGION", "us-east-1")
        ec2 = boto3.client("ec2", region_name=region)
        paginator = ec2.get_paginator("describe_instances")

        filters = [
            {"Name": "instance-state-name", "Values": ["stopped"]},
        ]
        if cluster_name:
            filters.append({"Name": "tag:ray-cluster-name", "Values": [cluster_name]})
        else:
            filters.append({"Name": "tag-key", "Values": ["ray-cluster-name"]})

        pages = paginator.paginate(Filters=filters)
        result = []
        for page in pages:
            for res in page["Reservations"]:
                for inst in res["Instances"]:
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    result.append({
                        "instance_id": inst["InstanceId"],
                        "cluster_name": tags.get("ray-cluster-name", "unknown"),
                    })
        return result

    def terminate_by_ids(self, config, ids):
        import boto3
        if not ids:
            return 0
        region = config.get("AWS_REGION", "us-east-1")
        ec2 = boto3.client("ec2", region_name=region)
        ec2.terminate_instances(InstanceIds=ids)
        return len(ids)

    def bake_hint(self, config):
        from brr.templates import global_setup_hash
        bake_hash = config.get("BAKE_SETUP_HASH", "")
        has_baked = config.get("AMI_UBUNTU_BAKED") or config.get("AMI_DL_BAKED")
        if has_baked and bake_hash and bake_hash != global_setup_hash():
            return (
                "Warning: setup.sh has changed since last bake. "
                "Run `brr bake aws` to rebuild."
            )
        elif not has_baked:
            return "Tip: Run `brr bake aws` to pre-bake setup into AMIs for faster boot."
        return None
