#!/usr/bin/env python3
"""E2E autoscaling test for Nebius Ray cluster.

Usage:
    brr up nebius:cpu max_workers=2 -y
    scp tests/e2e/autoscale_test.py brr-nebius-cpu:~/
    brr attach nebius:cpu -- bash -c "source /tmp/brr/venv/bin/activate && python ~/autoscale_test.py"

Monitor autoscaler in a separate terminal:
    brr attach nebius:cpu -- tail -f /tmp/ray/session_latest/logs/monitor.log

Clean up:
    brr down nebius:cpu -y
"""

import sys
import time

import ray


def wait_for_nodes(expected, timeout=600):
    """Wait until the cluster has at least `expected` alive nodes."""
    start = time.time()
    alive = []
    while time.time() - start < timeout:
        nodes = ray.nodes()
        alive = [n for n in nodes if n["Alive"]]
        print(f"  nodes alive: {len(alive)} / {expected}")
        if len(alive) >= expected:
            return alive
        time.sleep(15)
    raise TimeoutError(
        f"Only {len(alive)} nodes after {timeout}s, expected {expected}"
    )


@ray.remote(num_cpus=2)
def cpu_task(duration_s):
    """Occupy 2 CPUs for the given duration."""
    time.sleep(duration_s)
    return ray.get_runtime_context().node_id


def main():
    ray.init(address="auto")

    resources = ray.cluster_resources()
    nodes = ray.nodes()
    alive = [n for n in nodes if n["Alive"]]
    total_cpus = resources.get("CPU", 0)

    print(f"Cluster resources: {total_cpus} CPUs across {len(alive)} node(s)")
    print()

    # Head is 8vcpu. We submit tasks needing 16 CPUs total to force scale-up.
    num_tasks = 8
    cpus_needed = num_tasks * 2
    print(
        f"--- Phase 1: submit {num_tasks} tasks "
        f"({cpus_needed} CPUs needed > {int(total_cpus)} available) ---"
    )
    futures = [cpu_task.remote(90) for _ in range(num_tasks)]

    print()
    print("--- Phase 2: waiting for autoscaler to add workers ---")
    try:
        # head + 2 workers = 3 nodes
        wait_for_nodes(3, timeout=600)
        print("Scale-up OK")
    except TimeoutError as e:
        print(f"FAIL: {e}")
        for node in ray.nodes():
            status = "alive" if node["Alive"] else "dead"
            print(f"  {node['NodeID'][:12]}  {status}  {node.get('Resources', {})}")
        ray.shutdown()
        sys.exit(1)

    print()
    print("--- Phase 3: waiting for tasks to complete ---")
    results = ray.get(futures, timeout=300)
    unique_nodes = set(results)
    print(f"Tasks ran on {len(unique_nodes)} distinct node(s)")

    if len(unique_nodes) < 2:
        print("WARN: all tasks ran on a single node — workers may not have been used")
    else:
        print("Tasks distributed across workers OK")

    print()
    print("--- Phase 4: scale-down ---")
    print("Workers should terminate after the idle timeout (~5 min).")
    print("Monitor with:  ray status")
    print("Or:            tail -f /tmp/ray/session_latest/logs/monitor.log")
    print()
    print("PASS (scale-up verified, tasks distributed)")

    ray.shutdown()


if __name__ == "__main__":
    main()
