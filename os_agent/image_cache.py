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
_LOCK = threading.Lock()


def _key(png_b64: str) -> str:
    # hash the b64 so we don't hold the full string as a dict key
    return hashlib.sha1(png_b64.encode("ascii")).hexdigest()[:16]


def get_image(png_b64: str) -> Image.Image:
    """Return a decoded RGB PIL Image for png_b64, cached on identity."""
    k = _key(png_b64)
    with _LOCK:
        img = _CACHE.get(k)
        if img is not None:
            _CACHE.move_to_end(k)
            return img
    img = Image.open(io.BytesIO(base64.b64decode(png_b64))).convert("RGB")
    with _LOCK:
        _CACHE[k] = img
        _CACHE.move_to_end(k)
        if len(_CACHE) > _MAX_ENTRIES:
            _CACHE.popitem(last=False)
    return img


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
