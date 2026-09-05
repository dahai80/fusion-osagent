"""Multi-node coordination (audit gap 2 / enterprise fleet).

N osagent nodes on one host share a single mlx cluster (localhost:11434).
Without coordination each node independently hammers the cluster: each has
its own circuit breaker that only sees ITS OWN failures, so 4 nodes each
below their own threshold still flood a struggling cluster (4×5=20 fails
before any one opens). And there is no registry of who is running.

This module gives a local-first coordination plane (no Redis / etcd — Apple
Silicon local fleet, single host):

  NodeRegistry  — nodes register/deregister; snapshot shows the live fleet.
  ClusterHealth — shared cluster-level failure signal. Each node reports its
                  mlx failures to a shared JSON file under an advisory flock;
                  any node whose aggregate-failure check trips tells its local
                  breaker to open. This turns N independent breakers into one
                  cluster-wide breaker without a central controller.

Coordination is file-based (one state file under ~/.fusion-osagent/cluster/),
guarded by fcntl.flock. Stale nodes (heartbeat older than ttl) are reaped.
100% local; no network dependency.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from fusion_core import get_logger

log = get_logger("os_agent.coordination")


def _default_state_dir() -> str:
    return os.environ.get("OSA_CLUSTER_DIR") or os.path.join(
        os.path.expanduser("~"), ".fusion-osagent", "cluster"
    )


class _FileLock:
    """Advisory flock on a sidecar lock file. Reentrant-safe per process via a
    process-wide set of held paths (a second acquire in the same process is a
    no-op)."""

    _held: set[str] = set()
    _held_lock = None

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fh = None

    def __enter__(self):
        if self.lock_path in _FileLock._held:
            return self
        Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        _FileLock._held.add(self.lock_path)
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            _FileLock._held.discard(self.lock_path)


@dataclass
class NodeInfo:
    node_id: str
    started_at: float
    last_heartbeat: float
    meta: dict


class NodeRegistry:
    """File-shared registry of live osagent nodes on this host."""

    def __init__(self, state_path: str | None = None, heartbeat_ttl_s: float = 60.0) -> None:
        self.state_path = state_path or os.path.join(_default_state_dir(), "nodes.json")
        self.lock_path = self.state_path + ".lock"
        self.heartbeat_ttl_s = heartbeat_ttl_s

    def _read(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {"nodes": {}}

    def _write(self, data: dict) -> None:
        Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def register(self, node_id: str, **meta) -> NodeInfo:
        now = time.time()
        info = NodeInfo(node_id=node_id, started_at=now, last_heartbeat=now, meta=meta)
        with _FileLock(self.lock_path):
            data = self._read()
            data.setdefault("nodes", {})[node_id] = asdict(info)
            self._write(data)
        log.info("node registered: %s meta=%s", node_id, meta)
        return info

    def heartbeat(self, node_id: str) -> None:
        now = time.time()
        with _FileLock(self.lock_path):
            data = self._read()
            node = data.get("nodes", {}).get(node_id)
            if node:
                node["last_heartbeat"] = now
                self._write(data)

    def deregister(self, node_id: str) -> None:
        with _FileLock(self.lock_path):
            data = self._read()
            data.get("nodes", {}).pop(node_id, None)
            self._write(data)
        log.info("node deregistered: %s", node_id)

    def live_nodes(self) -> list[NodeInfo]:
        now = time.time()
        with _FileLock(self.lock_path):
            data = self._read()
            nodes = data.get("nodes", {})
            # reap stale
            live = {}
            for nid, n in nodes.items():
                if (now - n.get("last_heartbeat", 0)) <= self.heartbeat_ttl_s:
                    live[nid] = n
            if len(live) != len(nodes):
                data["nodes"] = live
                self._write(data)
            return [NodeInfo(**n) for n in live.values()]


@dataclass
class ClusterHealth:
    """Shared cluster-level mlx health signal across nodes."""

    state_path: str
    lock_path: str
    window_s: float = 30.0
    open_threshold: int = 10  # aggregate failures in window -> cluster open

    def _read(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {"failures": []}

    def _write(self, data: dict) -> None:
        Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def report_failure(self, node_id: str) -> dict:
        now = time.time()
        with _FileLock(self.lock_path):
            data = self._read()
            fails = data.get("failures", [])
            fails.append({"ts": now, "node": node_id})
            cutoff = now - self.window_s
            fails = [f for f in fails if f["ts"] >= cutoff]
            data["failures"] = fails
            self._write(data)
            return {"window_failures": len(fails), "cluster_open": len(fails) >= self.open_threshold}

    def report_success(self, node_id: str) -> None:
        # success trims this node's recent failures so a recovering cluster
        # clears the aggregate signal quickly.
        now = time.time()
        with _FileLock(self.lock_path):
            data = self._read()
            fails = data.get("failures", [])
            cutoff = now - self.window_s
            fails = [f for f in fails if f["ts"] >= cutoff and f["node"] != node_id]
            data["failures"] = fails
            self._write(data)

    def should_open(self) -> bool:
        now = time.time()
        with _FileLock(self.lock_path):
            data = self._read()
            fails = [f for f in data.get("failures", []) if f["ts"] >= now - self.window_s]
            return len(fails) >= self.open_threshold

    def snapshot(self) -> dict:
        now = time.time()
        with _FileLock(self.lock_path):
            data = self._read()
            fails = [f for f in data.get("failures", []) if f["ts"] >= now - self.window_s]
            return {"window_failures": len(fails), "cluster_open": len(fails) >= self.open_threshold}


def build_cluster_health(state_path: str | None = None) -> ClusterHealth:
    return ClusterHealth(
        state_path=state_path or os.path.join(_default_state_dir(), "health.json"),
        lock_path=(state_path or os.path.join(_default_state_dir(), "health.json")) + ".lock",
    )
