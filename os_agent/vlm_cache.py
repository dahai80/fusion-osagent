"""VLM result cache (P5 / B4).

Repeated decide() calls with an unchanged screen + same query re-infer the
same image through the VLM — pure waste (the mlx-side KV cache only helps when
the prompt string is byte-identical, and the recorder/replayer loop re-issues
near-identical prompts between user turns). This cache short-circuits the
inference when the inputs are identical within a short TTL.

Key = (model, prompt, image_b64 hash). The image hash is the sha1 prefix of
the b64 string (same scheme as image_cache), so a changed screenshot →
different hash → guaranteed miss (no stale results). The prompt carries the
goal + recent history, so a changed history → different prompt → miss.

TTL bounds staleness even if hashes collide (they won't) and lets a repeated
identical call skip inference for a few seconds. ttl=0 disables the cache.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from fusion_core import get_logger

log = get_logger("os_agent.vlm_cache")


@dataclass
class _Entry:
    value: dict | None
    expires_at: float


class VlmCache:
    """Bounded LRU + TTL cache for chat_json results."""

    def __init__(self, ttl: float = 3.0, max_entries: int = 32) -> None:
        self.ttl = ttl
        self._max = max_entries
        self._store: OrderedDict[tuple, _Entry] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def _key(self, model: str, prompt: str, image_b64: str) -> tuple:
        # E3: hash the prompt too — slow_plan prompts embed the full history
        # blob (several KB). Storing the raw string as a dict key wasted memory
        # and made every get/put hash kilobytes. Same sha1[:16] scheme as image.
        prompt_hash = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:16] if prompt else ""
        img_hash = hashlib.sha1(image_b64.encode("ascii")).hexdigest()[:16] if image_b64 else ""
        return (model, prompt_hash, img_hash)

    def get(self, model: str, prompt: str, image_b64: str) -> tuple[dict | None, bool]:
        """Return (value, hit). hit=False means caller must infer."""
        if self.ttl <= 0:
            self.misses += 1
            return None, False
        k = self._key(model, prompt, image_b64)
        entry = self._store.get(k)
        if entry is None:
            self.misses += 1
            return None, False
        if time.monotonic() > entry.expires_at:
            self._store.pop(k, None)
            self.misses += 1
            return None, False
        self._store.move_to_end(k)
        self.hits += 1
        log.info("vlm cache HIT (model=%s hits=%d misses=%d)", model, self.hits, self.misses)
        return entry.value, True

    def put(self, model: str, prompt: str, image_b64: str, value: dict | None) -> None:
        if self.ttl <= 0:
            return
        k = self._key(model, prompt, image_b64)
        self._store[k] = _Entry(value=value, expires_at=time.monotonic() + self.ttl)
        self._store.move_to_end(k)
        if len(self._store) > self._max:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0
