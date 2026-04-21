"""Persistent Ray tag store for the Verda provider.

Verda instances have no native tag/label field, so the node provider
persists Ray tags to a local JSON file. Readers/writers coordinate via
``fcntl.flock`` (process-level) and ``threading.RLock`` (in-process).

On the head node the store lives at ``/tmp/verda-tags.json`` (set via
``VERDA_TAG_STORE_PATH``) to avoid NFS/flock issues on shared mounts.
"""

import errno
import fcntl
import json
import os
import threading
from pathlib import Path


def _default_path():
    env = os.environ.get("VERDA_TAG_STORE_PATH")
    if env:
        return Path(env)
    from brr.state import STATE_DIR
    return STATE_DIR / "verda-tags.json"


class VerdaTagStore:
    """JSON-backed tag map: ``{instance_id: {tag: value}}``."""

    def __init__(self, path=None):
        self._path = Path(path) if path else _default_path()
        self._rlock = threading.RLock()

    @property
    def path(self):
        return self._path

    def _read_unlocked(self, fh):
        fh.seek(0)
        data = fh.read()
        if not data.strip():
            return {"version": 1, "nodes": {}}
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return {"version": 1, "nodes": {}}
        if not isinstance(parsed, dict) or "nodes" not in parsed:
            return {"version": 1, "nodes": {}}
        return parsed

    def _write_unlocked(self, fh, state):
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state, indent=2, sort_keys=True))
        fh.write("\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass

    def _open(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        return fh

    def _lock(self, fh, exclusive):
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fh, flags)
        except OSError as e:
            # Some filesystems (NFS without lockd) don't support flock.
            # Fall back to in-process locking only.
            if e.errno in (errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOSYS):
                return False
            raise
        return True

    def _unlock(self, fh):
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass

    def get(self, node_id):
        with self._rlock:
            fh = self._open()
            locked = self._lock(fh, exclusive=False)
            try:
                state = self._read_unlocked(fh)
            finally:
                if locked:
                    self._unlock(fh)
                fh.close()
        return dict(state["nodes"].get(node_id, {}))

    def set(self, node_id, tags):
        """Replace all tags for node_id with the given mapping."""
        with self._rlock:
            fh = self._open()
            locked = self._lock(fh, exclusive=True)
            try:
                state = self._read_unlocked(fh)
                state["nodes"][node_id] = dict(tags)
                self._write_unlocked(fh, state)
            finally:
                if locked:
                    self._unlock(fh)
                fh.close()

    def update(self, node_id, tags):
        """Merge tags into existing entry (or create one)."""
        with self._rlock:
            fh = self._open()
            locked = self._lock(fh, exclusive=True)
            try:
                state = self._read_unlocked(fh)
                merged = dict(state["nodes"].get(node_id, {}))
                merged.update(tags)
                state["nodes"][node_id] = merged
                self._write_unlocked(fh, state)
            finally:
                if locked:
                    self._unlock(fh)
                fh.close()

    def delete(self, node_id):
        with self._rlock:
            fh = self._open()
            locked = self._lock(fh, exclusive=True)
            try:
                state = self._read_unlocked(fh)
                if node_id in state["nodes"]:
                    del state["nodes"][node_id]
                    self._write_unlocked(fh, state)
            finally:
                if locked:
                    self._unlock(fh)
                fh.close()

    def prune(self, live_ids):
        """Drop entries whose node_id is not in live_ids. Returns count pruned."""
        live = set(live_ids)
        with self._rlock:
            fh = self._open()
            locked = self._lock(fh, exclusive=True)
            try:
                state = self._read_unlocked(fh)
                removed = [nid for nid in state["nodes"] if nid not in live]
                if removed:
                    for nid in removed:
                        del state["nodes"][nid]
                    self._write_unlocked(fh, state)
                return len(removed)
            finally:
                if locked:
                    self._unlock(fh)
                fh.close()

    def all(self):
        with self._rlock:
            fh = self._open()
            locked = self._lock(fh, exclusive=False)
            try:
                state = self._read_unlocked(fh)
            finally:
                if locked:
                    self._unlock(fh)
                fh.close()
        return {nid: dict(tags) for nid, tags in state["nodes"].items()}
