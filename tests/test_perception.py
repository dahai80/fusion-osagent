"""Perception dual-track tests — AX hit, AX miss→visual fallback, coordinate convert."""
from __future__ import annotations

import pytest

from os_agent.adapters.executor import StubExecutorAdapter
from os_agent.adapters.mlx import StubMlxAdapter
from os_agent.config import OsaConfig
from os_agent.perception import Perception


@pytest.mark.asyncio
async def test_ax_track_hits_button():
    cfg = OsaConfig(stub_mode=True)
    p = Perception(cfg, StubExecutorAdapter(cfg), StubMlxAdapter(cfg))
    pr = await p.locate("OK")
    assert pr.track == "ax"
    assert pr.locator.x is not None
    assert pr.confidence >= 0.9


@pytest.mark.asyncio
async def test_visual_fallback_when_ax_miss():
    cfg = OsaConfig(stub_mode=True)
    ex = StubExecutorAdapter(cfg)
    p = Perception(cfg, ex, StubMlxAdapter(cfg))
    pr = await p.locate("nonexistent-button-xyz")
    assert pr.track == "visual"
    assert pr.locator.kind == "visual"


@pytest.mark.asyncio
async def test_capture_returns_ax_tree():
    cfg = OsaConfig(stub_mode=True)
    p = Perception(cfg, StubExecutorAdapter(cfg), StubMlxAdapter(cfg))
    shot = await p.capture(prefer_ax=True)
    assert shot.node_tree is not None
    assert shot.has_ax is True


@pytest.mark.asyncio
async def test_coordinate_point_to_pixel_conversion():
    from os_agent.config import pixels_to_points, points_to_pixels

    px, py = points_to_pixels(100.0, 200.0, 2.0)
    assert (px, py) == (200.0, 400.0)
    bx, by = pixels_to_points(200.0, 400.0, 2.0)
    assert (bx, by) == (100.0, 200.0)
