"""Post-action frame assertion (PRD F2.1 / Phase 1.2).

Capture before/after frames around an action, diff them, and assert the
expected state change occurred. Pixel diff catches "nothing happened" (miss);
optional VLM verify catches semantic mismatch ("clicked OK but dialog stayed").

Integrates into DesktopAgent: each mutating action can be wrapped with
assert_changed so a silent no-op is never treated as success (Rule 12).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fusion_core import get_logger
from PIL import Image, ImageChops

from os_agent.adapters.base import Screenshot
from os_agent.adapters.mlx import MlxAdapter, StubMlxAdapter
from os_agent.config import OsaConfig

log = get_logger("os_agent.action")

DIFF_THRESHOLD = 0.002  # fraction of changed pixels to count as "changed"


@dataclass
class FrameAssertion:
    ok: bool
    changed: bool
    changed_ratio: float
    error: str | None = None
    meta: dict = field(default_factory=dict)


class FrameAsserter:
    """Pixel-diff frame assertion with optional VLM semantic verify."""

    def __init__(self, cfg: OsaConfig, mlx: MlxAdapter | StubMlxAdapter) -> None:
        self.cfg = cfg
        self.mlx = mlx

    async def assert_changed(
        self,
        before: Screenshot,
        after: Screenshot,
        expected: str | None = None,
        threshold: float = DIFF_THRESHOLD,
    ) -> FrameAssertion:
        if not before.png_b64 or not after.png_b64:
            log.warning("assert_changed: missing frame(s) before=%s after=%s", bool(before.png_b64), bool(after.png_b64))
            return FrameAssertion(ok=False, changed=False, changed_ratio=0.0, error="missing frame")
        ratio = self._diff_ratio(before.png_b64, after.png_b64)
        changed = ratio >= threshold
        log.info("assert_changed: ratio=%.5f threshold=%.5f changed=%s", ratio, threshold, changed)
        if not changed:
            return FrameAssertion(ok=False, changed=False, changed_ratio=ratio, error="no pixel change detected")
        if expected:
            return await self._verify_semantic(after, expected, ratio)
        return FrameAssertion(ok=True, changed=True, changed_ratio=ratio)

    def _diff_ratio(self, a_b64: str, b_b64: str) -> float:
        try:
            from os_agent import image_cache

            ia = image_cache.get_image(a_b64)
            ib = image_cache.get_image(b_b64)
            if ia.size != ib.size:
                log.info("diff: size mismatch %s vs %s — resize compare", ia.size, ib.size)
                ib = ib.resize(ia.size, Image.LANCZOS)
            # P3: downsample to a 256px-wide thumbnail before diff. Large Retina
            # frames (3160x1964) cost tens of ms per full-res histogram; the
            # change/no-change decision is stable at thumbnail resolution and
            # the ratio is preserved (both frames scaled identically).
            THUMB_W = 256
            if ia.size[0] > THUMB_W:
                scale = THUMB_W / ia.size[0]
                ia = ia.resize((THUMB_W, max(1, int(ia.size[1] * scale))), Image.LANCZOS)
                ib = ib.resize(ia.size, Image.LANCZOS)
            diff = ImageChops.difference(ia, ib)
            if diff.getbbox() is None:
                return 0.0
            # D7: count the actual changed pixels via the grayscale histogram,
            # not the bounding-box area. A tiny change inside a large bbox was
            # grossly over-counted before (e.g. a 10px cursor move reported as
            # a 50% frame change). Non-zero luminance bins = changed pixels.
            hist = diff.convert("L").histogram()  # 256 bins, index = luminance
            total = ia.size[0] * ia.size[1]
            if not total:
                return 0.0
            changed_px = sum(hist[1:])  # bin 0 = identical pixels
            return min(1.0, changed_px / total)
        except Exception as e:
            log.error("diff compute failed: %s", e)
            return 0.0

    async def _verify_semantic(self, after: Screenshot, expected: str, ratio: float) -> FrameAssertion:
        prompt = (
            f"You are a GUI state verifier. The user performed an action expecting: "
            f"'{expected}'. Look at this screenshot and return ONLY JSON: "
            f'{{"match": true|false, "reason": "<short>"}}.'
        )
        try:
            data = await self.mlx.chat_json(prompt, after.png_b64)
        except Exception as e:
            # D7 fail-loud: a verifier exception must NOT be masked as success.
            log.warning("semantic verify failed: %s — fail-loud (ok=False)", e)
            return FrameAssertion(ok=False, changed=True, changed_ratio=ratio, error=f"verify error: {e}", meta={"expected": expected, "verify_error": str(e)})
        if data is None:
            log.warning("semantic verify: non-JSON response — fail-loud (ok=False)")
            return FrameAssertion(ok=False, changed=True, changed_ratio=ratio, error="verify error: non-JSON", meta={"expected": expected})
        match = bool(data.get("match", False))
        return FrameAssertion(
            ok=match,
            changed=True,
            changed_ratio=ratio,
            meta={"expected": expected, "reason": data.get("reason")},
            error=None if match else "semantic mismatch",
        )
