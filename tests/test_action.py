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
