"""Shared decoded-image cache (P1/P2).

mask / som / diff each independently base64-decode + Image.open the same
screenshot per VLM call. A single Retina frame (3160x1964) costs tens of ms
to decode; decoding it 3x per perception cycle blows the <150ms budget. This
module caches the decoded PIL Image keyed by the b64 string identity, so the
first caller pays the decode and the rest reuse it.

The cache is bounded (LRU) and keyed on the b64 content hash prefix — b64
strings are large, so we hash them rather than store the key verbatim.

E2: the OrderedDict is mutated from worker threads (mask/som/diff run under
asyncio.to_thread), so all access is serialized under a lock — unguarded
move_to_end / popitem during concurrent get_image calls corrupted the LRU
order and could raise on CPython. One process currently hosts one
DesktopAgent, so cross-instance LRU eviction is not a live risk; if multi-
instance arrives, give each DesktopAgent its own ImageCache instance.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
from collections import OrderedDict

from fusion_core import get_logger
from PIL import Image

log = get_logger("os_agent.image_cache")

_CACHE: OrderedDict[str, Image.Image] = OrderedDict()
_MAX_ENTRIES = 8
# P1 perf: a Retina frame decoded to RGB is ~18MB; an unbounded entry count
# lets the cache hold hundreds of MB. Cap total held pixels so a long session
# with many distinct frames cannot grow the cache toward OOM. Eviction is LRU.
_MAX_BYTES = 192 * 1024 * 1024  # 192 MiB ceiling on decoded pixel memory
_LOCK = threading.Lock()
_HITS = 0
_MISSES = 0


def _img_bytes(img) -> int:
    # decoded PIL RGB size = width * height * bands
    return img.width * img.height * len(img.getbands())


def configure(max_entries: int) -> None:
    """A5: raise the bound from the 8-entry default. 8 thrashes a single
    perception cycle (capture/mask/som/diff-before/diff-after = 5+ frames) and
    evicts hot frames across concurrent DesktopAgent instances. Called once at
    DesktopAgent init from cfg.image_cache_max_entries.
    """
    global _MAX_ENTRIES
    with _LOCK:
        if max_entries > _MAX_ENTRIES:
            _MAX_ENTRIES = max_entries
            log.info("image_cache max_entries raised to %d", _MAX_ENTRIES)


def _key(png_b64: str) -> str:
    # P2 perf: full sha1 hexdigest — the old [:16] truncation raised collision
    # odds and could return a stale decoded image for a different frame.
    return hashlib.sha1(png_b64.encode("ascii")).hexdigest()


def get_image(png_b64: str) -> Image.Image:
    """Return a decoded RGB PIL Image for png_b64, cached on identity."""
    global _HITS, _MISSES
    k = _key(png_b64)
    with _LOCK:
        img = _CACHE.get(k)
        if img is not None:
            _CACHE.move_to_end(k)
            _HITS += 1
            return img
        _MISSES += 1
    img = Image.open(io.BytesIO(base64.b64decode(png_b64))).convert("RGB")
    with _LOCK:
        _CACHE[k] = img
        _CACHE.move_to_end(k)
        # evict by entry count AND by total decoded-pixel bytes — whichever
        # trips first — so the cache cannot grow toward OOM on a long session.
        total = sum(_img_bytes(v) for v in _CACHE.values())
        while _CACHE and (len(_CACHE) > _MAX_ENTRIES or total > _MAX_BYTES):
            _evicted_k, evicted = _CACHE.popitem(last=False)
            total -= _img_bytes(evicted)
    return img


def stats() -> dict:
    """E5: cache hit/miss counters for observability export."""
    with _LOCK:
        return {"hits": _HITS, "misses": _MISSES, "entries": len(_CACHE), "max_entries": _MAX_ENTRIES}


def clear() -> None:
    global _HITS, _MISSES
    with _LOCK:
        _CACHE.clear()
        _HITS = 0
        _MISSES = 0
