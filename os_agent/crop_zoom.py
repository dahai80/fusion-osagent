"""Patch-level crop & zoom (PRD Phase 2.3).

Crop a region around a candidate locator and (optionally) upscale it so the
VLM grounds small/dense controls (toolbar icons, compact list rows) at higher
effective resolution than a full-screen pass. Pure image op; no model call
here — the caller feeds the cropped b64 back into mlx.chat_json.

Coordinate space: works in physical pixels of the Screenshot (which are what
PIL decodes), converts the crop center back to logical points for the
returned locator.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from fusion_core import get_logger
from PIL import Image

from os_agent.adapters.base import Locator, Screenshot
from os_agent.config import OsaConfig, pixels_to_points

log = get_logger("os_agent.crop_zoom")


@dataclass
class CropResult:
    crop_b64: str
    crop_width: int          # upscaled px
    crop_height: int         # upscaled px
    origin_px: tuple[int, int]  # top-left of crop in screenshot physical px
    orig_width: int          # pre-upscale px
    orig_height: int         # pre-upscale px
    upscale: int


class CropZoomer:
    """Crop a patch around a point and upscale for finer VLM grounding."""

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg

    def crop_around(self, shot: Screenshot, center_px: tuple[float, float], half_extent_px: int = 120, upscale: int = 2) -> CropResult | None:
        if not shot.png_b64:
            log.warning("crop: empty screenshot")
            return None
        try:
            img = Image.open(io.BytesIO(base64.b64decode(shot.png_b64))).convert("RGB")
        except Exception as e:
            log.error("crop decode failed: %s", e)
            return None
        w, h = img.size
        cx, cy = int(center_px[0]), int(center_px[1])
        left = max(0, cx - half_extent_px)
        top = max(0, cy - half_extent_px)
        right = min(w, cx + half_extent_px)
        bottom = min(h, cy + half_extent_px)
        patch = img.crop((left, top, right, bottom))
        orig_w, orig_h = patch.size
        if upscale > 1 and orig_w > 0:
            patch = patch.resize((orig_w * upscale, orig_h * upscale), Image.BILINEAR)
        buf = io.BytesIO()
        patch.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        log.info("crop: origin=(%d,%d) orig=%s upscaled=%s x%d", left, top, (orig_w, orig_h), patch.size, upscale)
        return CropResult(
            crop_b64=b64,
            crop_width=patch.size[0],
            crop_height=patch.size[1],
            origin_px=(left, top),
            orig_width=orig_w,
            orig_height=orig_h,
            upscale=upscale,
        )

    def resolve_local_to_global(self, crop: CropResult, local_norm: tuple[float, float]) -> Locator:
        """Map a normalized (0-1) point in the crop back to a global logical-point Locator."""
        nx, ny = local_norm
        gx_px = crop.origin_px[0] + nx * crop.orig_width
        gy_px = crop.origin_px[1] + ny * crop.orig_height
        x_pt, y_pt = pixels_to_points(gx_px, gy_px, self.cfg.scale_factor)
        log.info("crop resolve: norm=(%.3f,%.3f) -> px=(%.1f,%.1f) -> pt=(%.1f,%.1f)", nx, ny, gx_px, gy_px, x_pt, y_pt)
        return Locator(kind="visual", x=x_pt, y=y_pt, raw={"crop_origin": list(crop.origin_px), "local_norm": [nx, ny]})
