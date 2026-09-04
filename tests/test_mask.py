"""Mask tests — sensitive AX regions blacked out, non-sensitive left intact."""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from os_agent.adapters.base import Screenshot
from os_agent.mask import SensitiveMasker


def _png(w: int = 200, h: int = 100, color=(255, 255, 255)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


TREE_WITH_PASSWORD = (
    '{"role":"AXWindow","children":['
    '{"role":"AXSecureTextField","label":"Password","frame":[10,10,80,20]},'
    '{"role":"AXButton","label":"OK","frame":[10,40,40,20]}'
    "]}"
)
TREE_PLAIN = (
    '{"role":"AXWindow","children":['
    '{"role":"AXButton","label":"OK","frame":[10,40,40,20]}'
    "]}"
)


def _pixel(img_b64: str, x: int, y: int) -> tuple:
    img = Image.open(io.BytesIO(base64.b64decode(img_b64)))
    return img.getpixel((x, y))


@pytest.mark.asyncio
async def test_mask_blacks_out_password_field():
    masker = SensitiveMasker()
    shot = Screenshot(png_b64=_png(200, 100), width=200, height=100, scale_factor=2.0, node_tree=TREE_WITH_PASSWORD)
    out = masker.mask(shot)
    assert out is not None
    # center of password field [10,10,80,20] -> (50, 20) should be black
    assert _pixel(out.png_b64, 50, 20) == (0, 0, 0)
    # OK button area [10,40,40,20] -> (30, 50) should remain white
    assert _pixel(out.png_b64, 30, 50) == (255, 255, 255)


@pytest.mark.asyncio
async def test_mask_no_sensitive_leaves_unchanged():
    masker = SensitiveMasker()
    shot = Screenshot(png_b64=_png(200, 100), width=200, height=100, scale_factor=2.0, node_tree=TREE_PLAIN)
    out = masker.mask(shot)
    assert out.png_b64 == shot.png_b64
    assert masker.masked_count == 0


@pytest.mark.asyncio
async def test_mask_label_hint_password_substring():
    masker = SensitiveMasker()
    tree = (
        '{"role":"AXWindow","children":['
        '{"role":"AXTextField","label":"Enter your password here","frame":[10,10,80,20]}'
        "]}"
    )
    shot = Screenshot(png_b64=_png(200, 100), width=200, height=100, scale_factor=2.0, node_tree=tree)
    out = masker.mask(shot)
    assert _pixel(out.png_b64, 50, 20) == (0, 0, 0)


@pytest.mark.asyncio
async def test_mask_no_ax_tree_returns_same():
    masker = SensitiveMasker()
    shot = Screenshot(png_b64=_png(), width=100, height=100, scale_factor=2.0, node_tree=None)
    out = masker.mask(shot)
    assert out.png_b64 == shot.png_b64
