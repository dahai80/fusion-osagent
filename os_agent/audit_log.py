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

    def __init__(
        self,
        path: str | None = None,
        agent_id: str = "osagent",
        buffer_max: int = 10000,
        rotate_max_bytes: int = 0,
        retention_files: int = 0,
        retention_days: int = 0,
    ) -> None:
        self.path = path
        self.agent_id = agent_id
        self._lock = threading.Lock()
        self._buffer: list[AuditEntry] = []
        self._buffer_max = buffer_max
        # GA ops: rotation + retention bounds. 0 = that bound disabled.
        self._rotate_max_bytes = rotate_max_bytes
        self._retention_files = retention_files
        self._retention_days = retention_days
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
                self._buffer = self._buffer[-self._buffer_max :]
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(entry.to_jsonl() + "\n")
                    # GA ops: rotate after the write if the active file exceeded
                    # the size cap. Rotation is best-effort: a failure logs but
                    # never breaks the action path (fail-open, same as the write).
                    if self._rotate_max_bytes > 0:
                        self._maybe_rotate_locked()
                except OSError as e:
                    # fail-open: never break the action path over an audit write
                    log.error("audit log write failed: %s (entry kind=%s)", e, kind)
        # P2 security: detail may carry a query/coords that should not land in
        # the general INFO stream (logs are broader-distribution than the
        # audit JSONL). Demote to debug and emit only a kind+key-count digest.
        log.debug("audit %s keys=%d", kind, len(detail))
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

    def query_disk(self, kind: str | None = None, since: float | None = None) -> list[AuditEntry]:
        # P2 fix: query() is in-memory only (lost on restart). This reads the
        # persisted JSONL so the CLI / ops can query the full durable trail.
        out: list[AuditEntry] = []
        if not self.path or not os.path.exists(self.path):
            return out
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        entry = AuditEntry(
                            ts=float(rec["ts"]),
                            agent_id=str(rec.get("agent_id", "")),
                            kind=str(rec.get("kind", "")),
                            detail=dict(rec.get("detail") or {}),
                        )
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue
                    if kind is not None and entry.kind != kind:
                        continue
                    if since is not None and entry.ts < since:
                        continue
                    out.append(entry)
        except OSError as e:
            log.error("audit log disk query failed: %s", e)
        return out

    def _maybe_rotate_locked(self) -> None:
        # GA ops: rotate the active JSONL to a timestamped archive when it
        # exceeds rotate_max_bytes, then prune archives by count + age. Called
        # under self._lock (held by record). time.time() is fine here — this is
        # runtime, not module load; the no-wall-clock rule is about deterministic
        # module-level values, and rotation is inherently wall-clock driven.
        try:
            if not self.path or not os.path.exists(self.path):
                return
            if os.path.getsize(self.path) < self._rotate_max_bytes:
                return
            base = self.path
            ts = time.strftime("%Y%m%d-%H%M%S")
            rotated = f"{base}.{ts}"
            os.rename(self.path, rotated)
            # truncate the active file (rename moved it; recreate empty)
            open(self.path, "a", encoding="utf-8").close()
            log.info("audit log rotated: %s -> %s", self.path, rotated)
            self._prune_archives_locked(base)
        except OSError as e:
            log.warning("audit log rotation failed: %s", e)

    def _prune_archives_locked(self, base: str) -> None:
        # Retention: keep at most retention_files archives; drop any older than
        # retention_days. Archives match f"{base}.*" (timestamped). Sort by mtime.
        if self._retention_files <= 0 and self._retention_days <= 0:
            return
        try:
            parent = os.path.dirname(base) or "."
            name = os.path.basename(base)
            archives = []
            for fn in os.listdir(parent):
                if fn.startswith(name + "."):
                    full = os.path.join(parent, fn)
                    if os.path.isfile(full):
                        archives.append(full)
            if not archives:
                return
            archives.sort(key=lambda p: os.path.getmtime(p))
            now = time.time()
            dropped = 0
            for full in archives:
                drop = False
                if self._retention_files > 0 and len(archives) - dropped > self._retention_files:
                    drop = True
                if self._retention_days > 0 and (now - os.path.getmtime(full)) > self._retention_days * 86400:
                    drop = True
                if drop:
                    try:
                        os.remove(full)
                        dropped += 1
                        log.info("audit archive pruned (retention): %s", full)
                    except OSError as e:
                        log.warning("audit archive prune failed: %s (%s)", e, full)
        except OSError as e:
            log.warning("audit archive retention scan failed: %s", e)

    def count(self) -> int:
        with self._lock:
            return len(self._buffer)

    def clear(self) -> None:
        # P2 security: clearing the audit buffer silently wiped integrity
        # evidence. Log it loudly so a clear is always visible in the trail.
        with self._lock:
            n = len(self._buffer)
            self._buffer.clear()
        log.warning("audit buffer cleared (%d entries dropped)", n)


def default_path() -> str:
    """Default audit JSONL under ~/.fusion-osagent/audit/."""
    base = os.environ.get("OSA_AUDIT_DIR") or os.path.join(os.path.expanduser("~"), ".fusion-osagent", "audit")
    return os.path.join(base, "osagent-audit.jsonl")
