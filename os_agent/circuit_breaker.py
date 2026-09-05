"""Circuit breaker (audit gap 3 / enterprise resilience).

A4's per-instance semaphore bounds concurrency but does NOT stop a failing mlx
cluster from being hammered: every request still goes out, each one waits the
full timeout, and a down cluster pins every agent slot. A circuit breaker
opens after N consecutive failures (or a failure-rate threshold within a
window) and fast-fails subsequent calls for a cooldown — letting the cluster
recover instead of being stomped. After cooldown, a probe call (half-open)
either re-closes the breaker or re-opens it.

States (deterministic code, Rule 5 — no model judgment):
  CLOSED   -> calls pass through; failures counted in a rolling window.
  OPEN     -> calls fail fast with CircuitOpenError for `cooldown_s`.
  HALF_OPEN-> first call after cooldown probes; success closes, fail re-opens.

Thread-safe. One breaker per backend (mlx). Configurable via OsaConfig.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from fusion_core import get_logger

log = get_logger("os_agent.circuit_breaker")


class CircuitOpenError(Exception):
    """Raised when the breaker is OPEN and a call is fast-failed."""


@dataclass
class BreakerConfig:
    failure_threshold: int = 5  # consecutive failures to open
    window_s: float = 30.0  # rolling window for rate-based opening
    failure_rate: float = 0.5  # open if >this fraction fail within window (min 10 calls)
    cooldown_s: float = 15.0  # OPEN -> HALF_OPEN after this long
    min_calls_for_rate: int = 10  # don't open on rate with fewer samples


class CircuitBreaker:
    def __init__(self, name: str = "mlx", cfg: BreakerConfig | None = None) -> None:
        self.name = name
        self.cfg = cfg or BreakerConfig()
        self._lock = threading.Lock()
        self._state = "closed"
        self._opened_at: float = 0.0
        self._consecutive_fail = 0
        self._window: deque[tuple[float, bool]] = deque()  # (ts, ok)
        # P1 fix: gate half-open so only ONE caller probes the cluster. Without
        # this, N concurrent callers in half_open all pass allow() and fire real
        # requests at a still-recovering cluster — defeating the cooldown.
        self._half_open_probe_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._effective_state()

    def _effective_state(self) -> str:
        # caller holds the lock
        if self._state == "open" and (time.monotonic() - self._opened_at) >= self.cfg.cooldown_s:
            self._state = "half_open"
            log.info("breaker %s: OPEN -> HALF_OPEN (cooldown elapsed)", self.name)
        return self._state

    def allow(self) -> bool:
        """Return True if a call may proceed; False (raise) if OPEN.

        In HALF_OPEN only the first caller proceeds (the probe); subsequent
        callers fast-fail until the probe resolves via on_success/on_failure.
        """
        with self._lock:
            st = self._effective_state()
            if st == "open":
                return False
            if st == "half_open":
                if self._half_open_probe_in_flight:
                    return False
                self._half_open_probe_in_flight = True
                return True
            return True

    def on_success(self) -> None:
        with self._lock:
            self._consecutive_fail = 0
            self._window_append(True)
            self._half_open_probe_in_flight = False
            if self._state == "half_open":
                self._state = "closed"
                log.info("breaker %s: HALF_OPEN -> CLOSED (probe succeeded)", self.name)

    def on_failure(self) -> None:
        with self._lock:
            self._consecutive_fail += 1
            self._window_append(False)
            self._half_open_probe_in_flight = False
            if self._state == "half_open":
                self._open("half-open probe failed")
                return
            if self._consecutive_fail >= self.cfg.failure_threshold:
                self._open(f"consecutive failures {self._consecutive_fail} >= {self.cfg.failure_threshold}")
                return
            # rate-based opening within the window
            ok = sum(1 for _, s in self._window if s)
            total = len(self._window)
            fail = total - ok
            if total >= self.cfg.min_calls_for_rate and (fail / total) > self.cfg.failure_rate:
                self._open(f"failure rate {fail / total:.2f} within {self.cfg.window_s}s window")

    def _open(self, reason: str) -> None:
        self._state = "open"
        self._opened_at = time.monotonic()
        log.warning("breaker %s: -> OPEN (%s)", self.name, reason)

    def _window_append(self, ok: bool) -> None:
        now = time.monotonic()
        self._window.append((now, ok))
        cutoff = now - self.cfg.window_s
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "state": self._effective_state(),
                "consecutive_fail": self._consecutive_fail,
                "window_size": len(self._window),
            }

    def reset(self) -> None:
        with self._lock:
            self._state = "closed"
            self._consecutive_fail = 0
            self._opened_at = 0.0
            self._half_open_probe_in_flight = False
            self._window.clear()
