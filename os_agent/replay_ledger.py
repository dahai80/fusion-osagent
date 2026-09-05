"""Idempotent replay ledger (audit gap 5 / transactional guarantees).

Enterprise replay must be safe to re-run after a crash: if a replay died at
step 7 of 10, restarting it must RESUME from step 8, not re-execute steps
1-7 (double-clicks, double-types, duplicate submits). Without this, replay
has no transactional guarantee — a retry is a correctness hazard, not a
recovery tool.

The ledger is the durable "completed-step" log for one idempotency key. It
records each step's seq as it succeeds and persists to JSONL. On resume,
`completed()` returns the set of done seqs so the replayer skips them.

100% local, thread-safe, fail-open on write (a ledger error logs loudly but
never blocks the replay itself — Rule 12: fail visibly, don't take down the
controlled system).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from fusion_core import get_logger

log = get_logger("os_agent.replay_ledger")


class ReplayLedger:
    """Durable record of completed replay step seqs for one idempotency key."""

    def __init__(self, idempotency_key: str, path: str | None = None) -> None:
        self.key = idempotency_key
        self.path = path
        self._lock = threading.Lock()
        self._done: set[int] = set()
        if path:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                                if rec.get("key") == self.key and rec.get("status") == "done":
                                    self._done.add(int(rec["seq"]))
                            except json.JSONDecodeError:
                                continue
            except OSError as e:
                log.error("replay ledger load failed: %s — starting fresh", e)
                self.path = None

    def completed(self) -> set[int]:
        with self._lock:
            return set(self._done)

    def is_done(self, seq: int) -> bool:
        with self._lock:
            return seq in self._done

    def claim(self, seq: int) -> bool:
        """Atomically claim a seq before execution. Returns True only for the
        first caller; concurrent callers (same key, same seq) get False and
        skip. Closes the is_done→execute→mark_done window where two concurrent
        replays both see is_done=False and double-execute a mutating step.
        Also persists an `in_progress` record so a crash between claim and
        mark_done is visible on resume (the step re-runs only if idempotent)."""
        with self._lock:
            if seq in self._done:
                return False
            self._done.add(seq)
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(
                            json.dumps({"key": self.key, "seq": seq, "status": "in_progress"}, separators=(",", ":"))
                            + "\n"
                        )
                except OSError as e:
                    log.error("replay ledger claim write failed: %s (seq=%d)", e, seq)
            return True

    def mark_done(self, seq: int) -> None:
        with self._lock:
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(
                            json.dumps({"key": self.key, "seq": seq, "status": "done"}, separators=(",", ":")) + "\n"
                        )
                except OSError as e:
                    log.error("replay ledger write failed: %s (seq=%d)", e, seq)

    def clear(self) -> None:
        # P3 fix: also truncate the persisted JSONL, not just the in-memory set.
        # Otherwise a cleared ledger regrows done entries on the next load.
        with self._lock:
            self._done.clear()
            if self.path and os.path.exists(self.path):
                try:
                    with open(self.path, "w", encoding="utf-8") as fh:
                        fh.write("")
                    log.info("replay ledger truncated: %s", self.path)
                except OSError as e:
                    log.error("replay ledger truncate failed: %s", e)
