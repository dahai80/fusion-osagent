"""SOM annotation tests — AX node extraction, mark drawing, index resolution."""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from os_agent.adapters.base import Screenshot
from os_agent.config import OsaConfig
from os_agent.som import SomAnnotator


def _png_b64(w: int = 200, h: int = 100) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


TREE = json_tree = (
    '{"role":"AXWindow","children":['
    '{"role":"AXButton","label":"OK","frame":[10,10,40,20]},'
    '{"role":"AXTextField","label":"Name","frame":[10,40,80,20]},'
    '{"role":"AXGroup","children":[{"role":"AXLink","label":"help","frame":[10,70,30,15]}]}'
    "]}"
)


@pytest.mark.asyncio
async def test_som_extracts_interactive_nodes():
    cfg = OsaConfig(stub_mode=True)
    ann = SomAnnotator(cfg)
    shot = Screenshot(png_b64=_png_b64(), width=200, height=100, scale_factor=2.0, node_tree=TREE)
    view = await ann.annotate(shot)
    roles = [n.role for n in view.nodes]
    assert roles == ["axbutton", "axtextfield", "axlink"]
    assert [n.index for n in view.nodes] == [1, 2, 3]


@pytest.mark.asyncio
async def test_som_draws_marked_image():
    cfg = OsaConfig(stub_mode=True)
    ann = SomAnnotator(cfg)
    shot = Screenshot(png_b64=_png_b64(200, 100), width=200, height=100, scale_factor=2.0, node_tree=TREE)
    view = await ann.annotate(shot)
    assert view.marked_b64 is not None
    img = Image.open(io.BytesIO(base64.b64decode(view.marked_b64)))
    assert img.size == (200, 100)


@pytest.mark.asyncio
async def test_som_resolve_index_to_locator():
    cfg = OsaConfig(stub_mode=True)
    ann = SomAnnotator(cfg)
    shot = Screenshot(png_b64=_png_b64(), width=200, height=100, scale_factor=2.0, node_tree=TREE)
    view = await ann.annotate(shot)
    loc = view.resolve(1, cfg.scale_factor)
    assert loc is not None
    assert loc.kind == "ax"
    assert loc.ax_label == "OK"
    cx, cy = loc.as_point()
    assert cx == pytest.approx((10 + 40 / 2) / 2.0)
    assert cy == pytest.approx((10 + 20 / 2) / 2.0)


@pytest.mark.asyncio
async def test_som_resolve_missing_index():
    cfg = OsaConfig(stub_mode=True)
    ann = SomAnnotator(cfg)
    shot = Screenshot(png_b64=_png_b64(), width=200, height=100, scale_factor=2.0, node_tree=TREE)
    view = await ann.annotate(shot)
    assert view.resolve(99, cfg.scale_factor) is None


@pytest.mark.asyncio
async def test_som_empty_ax_tree():
    # B7: no AX tree -> no phantom marks. marked_b64 is None (not the raw image
    # pretending to carry numbered boxes), so the Slow core knows SOM is absent.
    cfg = OsaConfig(stub_mode=True)
    ann = SomAnnotator(cfg)
    shot = Screenshot(png_b64=_png_b64(), width=200, height=100, scale_factor=2.0, node_tree=None)
    view = await ann.annotate(shot)
    assert view.nodes == []
    assert view.marked_b64 is None
