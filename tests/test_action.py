"""Frame assertion tests — pixel diff detects change / no-change / semantic verify."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from os_agent.action import FrameAsserter
from os_agent.adapters.base import Screenshot
from os_agent.adapters.mlx import StubMlxAdapter
from os_agent.config import OsaConfig


def _png(color: tuple[int, int, int] = (255, 255, 255), w: int = 100, h: int = 100) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.mark.asyncio
async def test_assert_changed_detects_change():
    cfg = OsaConfig(stub_mode=True)
    fa = FrameAsserter(cfg, StubMlxAdapter(cfg))
    before = Screenshot(png_b64=_png((255, 255, 255)), width=100, height=100, scale_factor=2.0)
    after = Screenshot(png_b64=_png((255, 0, 0)), width=100, height=100, scale_factor=2.0)
    res = await fa.assert_changed(before, after)
    assert res.ok is True
    assert res.changed is True
    assert res.changed_ratio > 0


@pytest.mark.asyncio
async def test_assert_changed_no_change_fails():
    cfg = OsaConfig(stub_mode=True)
    fa = FrameAsserter(cfg, StubMlxAdapter(cfg))
    shot = Screenshot(png_b64=_png((0, 0, 0)), width=100, height=100, scale_factor=2.0)
    res = await fa.assert_changed(shot, shot)
    assert res.ok is False
    assert res.changed is False
    assert res.error == "no pixel change detected"


@pytest.mark.asyncio
async def test_assert_changed_missing_frame():
    cfg = OsaConfig(stub_mode=True)
    fa = FrameAsserter(cfg, StubMlxAdapter(cfg))
    before = Screenshot(png_b64=None, width=100, height=100, scale_factor=2.0)
    after = Screenshot(png_b64=_png(), width=100, height=100, scale_factor=2.0)
    res = await fa.assert_changed(before, after)
    assert res.ok is False
    assert res.error == "missing frame"


@pytest.mark.asyncio
async def test_assert_changed_semantic_verify_uses_mlx():
    cfg = OsaConfig(stub_mode=True)
    mlx = StubMlxAdapter(cfg)
    fa = FrameAsserter(cfg, mlx)
    before = Screenshot(png_b64=_png((255, 255, 255)), width=100, height=100, scale_factor=2.0)
    after = Screenshot(png_b64=_png((0, 255, 0)), width=100, height=100, scale_factor=2.0)
    res = await fa.assert_changed(before, after, expected="dialog opens")
    assert res.changed is True
    assert "expected" in res.meta


@pytest.mark.asyncio
async def test_assert_changed_large_frame_thumbnail_diff():
    # P3: large Retina frame (3160x1964) is downsampled before diff; the
    # change/no-change decision must still be correct and fast.
    cfg = OsaConfig(stub_mode=True)
    fa = FrameAsserter(cfg, StubMlxAdapter(cfg))
    before = Screenshot(png_b64=_png((255, 255, 255), w=3160, h=1964), width=3160, height=1964, scale_factor=2.0)
    after = Screenshot(png_b64=_png((0, 0, 0), w=3160, h=1964), width=3160, height=1964, scale_factor=2.0)
    res = await fa.assert_changed(before, after)
    assert res.changed is True
    assert res.changed_ratio > 0.5


def test_image_cache_reuses_decoded_image():
    # P1/P2: same b64 decodes once; second get returns the cached PIL Image.
    from os_agent import image_cache

    image_cache.clear()
    b64 = _png((10, 20, 30))
    img1 = image_cache.get_image(b64)
    img2 = image_cache.get_image(b64)
    assert img1 is img2  # same object, no re-decode
    assert img1.size == (100, 100)
