"""Lightweight metrics / observability (audit E5 / enterprise gap 1).

Enterprise production has no monitoring = blind flying. This module gives
osagent a zero-dependency, thread-safe metrics core: counters, latency
histograms, and cache hit/miss tracking. DesktopAgent.metrics_snapshot()
returns a plain dict an external Prometheus exporter (or fusion-core
monitor) can scrape — local-first, no prometheus_client dependency.

Design (Rule 5: decide with code, not tokens):
- Counters are monotonic int accumulators.
- Histograms use fixed latency buckets (ms) + sum + count; no streaming
  quantile math that could drift or block.
- Cache tracking records hits/misses so the snapshot exposes a hit rate.
- One module-level registry; components grab it via get_registry(). A
  DesktopAgent can also hold its own instance for per-agent isolation in a
  multi-node fleet (gap 5).
- Thread-safe via a single Lock; metrics are hot but cheap (dict ops under
  a short critical section). No async lock needed — inc/observe are sync.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from fusion_core import get_logger

log = get_logger("os_agent.metrics")

# Latency histogram buckets in milliseconds (powers-of-ish, GUI-action scale).
LATENCY_BUCKETS_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)


@dataclass
class Histogram:
    name: str
    buckets: tuple[int, ...] = LATENCY_BUCKETS_MS
    counts: list[int] = field(default_factory=lambda: [0] * len(LATENCY_BUCKETS_MS))
    sum_ms: float = 0.0
    count: int = 0

    def observe(self, ms: float) -> None:
        self.sum_ms += ms
        self.count += 1
        for i, edge in enumerate(self.buckets):
            if ms <= edge:
                self.counts[i] += 1
                return
        # above the last bucket -> roll into the overflow tail (last cell)
        self.counts[-1] += 1

    def snapshot(self) -> dict:
        return {
            "buckets": list(zip([f"<= {b}" for b in self.buckets], self.counts, strict=False)),
            "sum_ms": round(self.sum_ms, 3),
            "count": self.count,
            "avg_ms": round(self.sum_ms / self.count, 3) if self.count else 0.0,
        }


@dataclass
class CacheStats:
    name: str
    hits: int = 0
    misses: int = 0

    def record_hit(self) -> None:
        self.hits += 1

    def record_miss(self) -> None:
        self.misses += 1

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return round(self.hits / self.total, 4) if self.total else 0.0

    def snapshot(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "hit_rate": self.hit_rate, "total": self.total}


class MetricsRegistry:
    """Thread-safe bag of counters + histograms + cache stats."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, Histogram] = {}
        self._caches: dict[str, CacheStats] = {}

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def counter(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def observe(self, name: str, ms: float) -> None:
        with self._lock:
            h = self._histograms.get(name)
            if h is None:
                h = Histogram(name=name)
                self._histograms[name] = h
            h.observe(ms)

    def cache_hit(self, name: str) -> None:
        with self._lock:
            c = self._caches.get(name)
            if c is None:
                c = CacheStats(name=name)
                self._caches[name] = c
            c.record_hit()

    def cache_miss(self, name: str) -> None:
        with self._lock:
            c = self._caches.get(name)
            if c is None:
                c = CacheStats(name=name)
                self._caches[name] = c
            c.record_miss()

    def cache_stats(self, name: str) -> CacheStats:
        with self._lock:
            c = self._caches.get(name)
            if c is None:
                c = CacheStats(name=name)
                self._caches[name] = c
            return c

    @contextmanager
    def time_call(self, name: str):
        """Context manager: observe wall-clock ms under the given histogram."""
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.observe(name, (time.monotonic() - t0) * 1000.0)

    def snapshot(self) -> dict:
        """Full export: counters, histograms, caches. Safe to JSON-serialize."""
        with self._lock:
            return {
                "counters": dict(self._counters),
                "histograms": {n: h.snapshot() for n, h in self._histograms.items()},
                "caches": {n: c.snapshot() for n, c in self._caches.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._caches.clear()


# Module-level singleton (default registry for single-node use).
_REG = MetricsRegistry()


def get_registry() -> MetricsRegistry:
    return _REG


def reset_registry() -> None:
    _REG.reset()
