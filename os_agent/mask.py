"""Sensitive-region masking (PRD F3.5 / Phase 1.5).

Mask password / secure-text / sensitive AX nodes before the screenshot reaches
the VLM. 100% local — no frame or input leaves the host unmasked. Done
client-side (pending upstream E3 which may mask in executor; until then we mask
here so privacy holds regardless of sibling behavior).

Masked regions are blacked out in the image; the AX label is kept (so the model
still sees "Password" exists) but the value never renders.
"""
from __future__ import annotations

import base64
import io
import json

from fusion_core import get_logger
from PIL import Image, ImageDraw

from os_agent.adapters.base import Screenshot

log = get_logger("os_agent.mask")

SENSITIVE_ROLES = {
    "axsecuretextfield", "axpasswordfield", "axsecuretextarea",
}
SENSITIVE_LABEL_HINTS = ("password", "passwd", "secret", "token", "api key", "credential", "pin")


class SensitiveMasker:
    """Black out sensitive AX regions in a screenshot before VLM use."""

    def __init__(self) -> None:
        self.masked_count = 0

    def mask(self, shot: Screenshot) -> Screenshot:
        if not shot.png_b64:
            return shot
        if not shot.node_tree:
            return shot
        regions = self._sensitive_regions(shot.node_tree)
        if not regions:
            return shot
        masked_b64 = self._blackout(shot.png_b64, regions)
        log.info("mask: %d sensitive region(s) masked", len(regions))
        self.masked_count += len(regions)
        return Screenshot(
            png_b64=masked_b64,
            width=shot.width,
            height=shot.height,
            scale_factor=shot.scale_factor,
            node_tree=shot.node_tree,
            meta={**shot.meta, "masked": len(regions)},
        )

    def _sensitive_regions(self, node_tree: str) -> list[list[float]]:
        try:
            tree = json.loads(node_tree)
        except json.JSONDecodeError as e:
            log.warning("mask: ax parse failed: %s", e)
            return []
        out: list[list[float]] = []
        _collect_sensitive(tree, out)
        return out

    def _blackout(self, png_b64: str, regions: list[list[float]]) -> str:
        img = Image.open(io.BytesIO(base64.b64decode(png_b64))).convert("RGB")
        draw = ImageDraw.Draw(img)
        for frame in regions:
            x, y, w, h = frame
            draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()


def _collect_sensitive(node: dict, out: list[list[float]]) -> None:
    role = str(node.get("role") or "").lower()
    label = str(node.get("label") or node.get("title") or "").lower()
    is_sensitive = role in SENSITIVE_ROLES or any(h in label for h in SENSITIVE_LABEL_HINTS)
    if is_sensitive:
        frame = node.get("frame") or node.get("position") or node.get("ax_frame")
        if frame and len(frame) >= 4:
            out.append([float(frame[0]), float(frame[1]), float(frame[2]), float(frame[3])])
    for child in node.get("children") or []:
        _collect_sensitive(child, out)
