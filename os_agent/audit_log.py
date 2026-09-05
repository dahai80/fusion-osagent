"""Structured audit log (audit gap 4 / enterprise compliance).

Enterprise production must answer "what did the agent see, mask, decide, and
do, and when?" — not from scattered log lines but a single structured record.
This module appends immutable AuditEntry rows to a JSONL file (one line per
event) and exposes a queryable in-memory buffer. 100% local; no frame or
cleartext value is ever recorded — only the *decision* metadata (which
regions were masked, which action ran, which core decided, latency, ok/fail).

Thread-safe (the executor runs action calls from async tasks that may share
threads). Fail-open for the audit sink itself: a write error is logged loudly
but never breaks the agent's action path (Rule 12: fail visibly, but do not
let the auditor take down the controlled system).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fusion_core import get_logger

log = get_logger("os_agent.audit_log")


@dataclass
class AuditEntry:
    ts: float
    agent_id: str
    kind: str  # capture | mask | decide | action | heal | replay | assert
    detail: dict = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class AuditLog:
    """Append-only structured audit trail.

    `path=None` keeps an in-memory buffer only (offline tests, ephemeral
    runs). A real path persists each entry to JSONL under a lock.
    """

    def __init__(self, path: str | None = None, agent_id: str = "osagent", buffer_max: int = 10000) -> None:
        self.path = path
        self.agent_id = agent_id
        self._lock = threading.Lock()
        self._buffer: list[AuditEntry] = []
        self._buffer_max = buffer_max
        if path:
            # fail-open: an unwritable/readonly audit path must never prevent
            # the agent from starting. Directory creation + writes happen under
            # guards; a failure logs loudly but keeps the in-memory buffer.
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log.error("audit log dir create failed: %s — in-memory only", e)
                self.path = None

    def record(self, kind: str, **detail) -> AuditEntry:
        entry = AuditEntry(ts=time.time(), agent_id=self.agent_id, kind=kind, detail=detail)
        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) > self._buffer_max:
                self._buffer = self._buffer[-self._buffer_max:]
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(entry.to_jsonl() + "\n")
                except OSError as e:
                    # fail-open: never break the action path over an audit write
                    log.error("audit log write failed: %s (entry kind=%s)", e, kind)
        log.info("audit %s: %s", kind, detail)
        return entry

    def query(self, kind: str | None = None, since: float | None = None) -> list[AuditEntry]:
        with self._lock:
            rows = list(self._buffer)
        out = []
        for r in rows:
            if kind is not None and r.kind != kind:
                continue
            if since is not None and r.ts < since:
                continue
            out.append(r)
        return out

    def count(self) -> int:
        with self._lock:
            return len(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


def default_path() -> str:
    """Default audit JSONL under ~/.fusion-osagent/audit/."""
    base = os.environ.get("OSA_AUDIT_DIR") or os.path.join(os.path.expanduser("~"), ".fusion-osagent", "audit")
    return os.path.join(base, "osagent-audit.jsonl")
