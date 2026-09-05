"""Patch-level crop & zoom tests (Phase 2.3)."""

from __future__ import annotations

import base64
import io

from PIL import Image, ImageDraw

from os_agent.adapters.base import Screenshot
from os_agent.config import OsaConfig
from os_agent.crop_zoom import CropZoomer


def _shot(size=(400, 300)) -> Screenshot:
    img = Image.new("RGB", size, (200, 200, 200))
    ImageDraw.Draw(img).rectangle([180, 140, 220, 160], fill=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Screenshot(
        png_b64=base64.b64encode(buf.getvalue()).decode(),
        width=size[0],
        height=size[1],
        scale_factor=2.0,
        node_tree=None,
    )


def test_crop_around_center():
    cz = CropZoomer(OsaConfig())
    res = cz.crop_around(_shot(), (200.0, 150.0), half_extent_px=50, upscale=2)
    assert res is not None
    assert res.crop_width == 100 * 2
    assert res.crop_height == 100 * 2
    assert res.upscale == 2
    assert res.origin_px == (150, 100)


def test_crop_clamps_at_edges():
    cz = CropZoomer(OsaConfig())
    res = cz.crop_around(_shot(), (0.0, 0.0), half_extent_px=50, upscale=1)
    assert res is not None
    assert res.origin_px == (0, 0)
    assert res.orig_width == 50
    assert res.orig_height == 50


def test_resolve_local_to_global_center():
    cz = CropZoomer(OsaConfig())
    res = cz.crop_around(_shot(), (200.0, 150.0), half_extent_px=50, upscale=2)
    loc = cz.resolve_local_to_global(res, (0.5, 0.5))
    # center: origin (150,100) + 0.5*orig(100,100) = (200,150) px -> /2 scale = (100, 75) pt
    assert loc.x == 100.0
    assert loc.y == 75.0


def test_resolve_local_to_global_corner():
    cz = CropZoomer(OsaConfig())
    res = cz.crop_around(_shot(), (200.0, 150.0), half_extent_px=50, upscale=2)
    loc = cz.resolve_local_to_global(res, (1.0, 1.0))
    # origin (150,100) + full orig (100,100) = (250,200) px -> (125, 100) pt
    assert loc.x == 125.0
    assert loc.y == 100.0


def test_crop_empty_screenshot():
    cz = CropZoomer(OsaConfig())
    res = cz.crop_around(Screenshot(png_b64=None, width=10, height=10, scale_factor=2.0, node_tree=None), (5.0, 5.0))
    assert res is None
