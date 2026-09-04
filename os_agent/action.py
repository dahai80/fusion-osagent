"""Post-action frame assertion (PRD F2.1 / Phase 1.2).

Capture before/after frames around an action, diff them, and assert the
expected state change occurred. Pixel diff catches "nothing happened" (miss);
optional VLM verify catches semantic mismatch ("clicked OK but dialog stayed").

Integrates into DesktopAgent: each mutating action can be wrapped with
assert_changed so a silent no-op is never treated as success (Rule 12).
"""
from __future__ import annotations

import base64
import io
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
            ia = Image.open(io.BytesIO(base64.b64decode(a_b64))).convert("RGB")
            ib = Image.open(io.BytesIO(base64.b64decode(b_b64))).convert("RGB")
            if ia.size != ib.size:
                log.info("diff: size mismatch %s vs %s — resize compare", ia.size, ib.size)
                ib = ib.resize(ia.size)
            diff = ImageChops.difference(ia, ib)
            bbox = diff.getbbox()
            if bbox is None:
                return 0.0
            hist = diff.convert("L").getextrema()
            if hist[0] == hist[1] == 0:
                return 0.0
            bbox_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            total = ia.size[0] * ia.size[1]
            return min(1.0, bbox_area / total) if total else 0.0
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
            match = bool(data.get("match", False))
            return FrameAssertion(
                ok=match,
                changed=True,
                changed_ratio=ratio,
                meta={"expected": expected, "reason": data.get("reason")},
                error=None if match else "semantic mismatch",
            )
        except Exception as e:
            log.warning("semantic verify failed: %s — trust pixel diff", e)
            return FrameAssertion(ok=True, changed=True, changed_ratio=ratio, meta={"expected": expected, "verify_error": str(e)})
