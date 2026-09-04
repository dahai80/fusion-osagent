"""Real-env integration tests (marked `integration`, skipped by default).

Run: pytest -m integration -v
Requires: fusion-mlx running with a VL model loaded
  (~/claude-home/fusion-mlx/start.sh start)
and API key in ~/.fusion-mlx/settings.json.
"""
from __future__ import annotations

import base64
import io
import json
import os

import pytest
from PIL import Image, ImageDraw

from os_agent.adapters.base import Screenshot
from os_agent.adapters.mlx import MlxAdapter
from os_agent.config import OsaConfig
from os_agent.mask import SensitiveMasker
from os_agent.perception import Perception
from os_agent.som import SomAnnotator

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("OSA_RUN_INTEGRATION"),
        reason="set OSA_RUN_INTEGRATION=1 to run real-env tests (needs fusion-mlx + VL model)",
    ),
]


def _make_frame(label_text: str = "OK", with_sensitive: bool = False) -> tuple[str, str | None]:
    img = Image.new("RGB", (400, 300), (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([160, 180, 260, 230], fill=(220, 40, 40))
    d.text((190, 195), label_text, fill=(255, 255, 255))
    if with_sensitive:
        d.rectangle([20, 20, 200, 50], fill=(200, 200, 200))
        d.text((30, 28), "Password", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    tree = None
    if with_sensitive:
        tree = json.dumps({"role": "AXWindow", "children": [
            {"role": "AXSecureTextField", "label": "Password", "frame": [20, 20, 180, 30]},
            {"role": "AXButton", "label": label_text, "frame": [160, 180, 100, 50]},
        ]})
    return b64, tree


@pytest.mark.asyncio
async def test_real_mlx_health():
    mlx = MlxAdapter(OsaConfig())
    try:
        assert await mlx.health() is True
    finally:
        await mlx.close()


@pytest.mark.asyncio
async def test_real_mlx_visual_grounding():
    mlx = MlxAdapter(OsaConfig())
    try:
        b64, _ = _make_frame("OK")
        prompt = (
            "You are a GUI grounding model. Find the OK button. "
            'Return ONLY JSON: {"x": <0-1 normalized>, "y": <0-1 normalized>, "confidence": <0-1>}.'
        )
        data = await mlx.chat_json(prompt, b64)
        assert "x" in data and "y" in data
        assert 0.0 <= float(data["x"]) <= 1.0
        assert 0.0 <= float(data["y"]) <= 1.0
        assert float(data.get("confidence", 0)) > 0.3
    finally:
        await mlx.close()


@pytest.mark.asyncio
async def test_real_perception_visual_locate():
    from os_agent.adapters.executor import StubExecutorAdapter

    cfg = OsaConfig()
    mlx = MlxAdapter(cfg)
    ex = StubExecutorAdapter(cfg, tree=None)  # no AX tree → force visual track
    p = Perception(cfg, ex, mlx)
    try:
        # StubExecutor returns 1x1 placeholder; feed real frame via capture override
        b64, _ = _make_frame("OK")
        shot = Screenshot(png_b64=b64, width=400, height=300, scale_factor=2.0, node_tree=None)
        pr = await p.locate("OK button", shot=shot)
        assert pr.track == "visual"
        assert pr.locator.x is not None
    finally:
        await mlx.close()
        await ex.close()


@pytest.mark.asyncio
async def test_real_som_annotate_real_image():
    cfg = OsaConfig()
    ann = SomAnnotator(cfg)
    b64, tree = _make_frame("OK", with_sensitive=True)
    shot = Screenshot(png_b64=b64, width=400, height=300, scale_factor=2.0, node_tree=tree)
    view = await ann.annotate(shot)
    assert view.marked_b64 is not None
    assert len(view.nodes) == 2  # password field + button
    roles = {n.role for n in view.nodes}
    assert "axsecuretextfield" in roles
    assert "axbutton" in roles


@pytest.mark.asyncio
async def test_real_mask_sensitive_blacked_out():
    masker = SensitiveMasker()
    b64, tree = _make_frame("OK", with_sensitive=True)
    shot = Screenshot(png_b64=b64, width=400, height=300, scale_factor=2.0, node_tree=tree)
    out = masker.mask(shot)
    assert out.meta.get("masked") == 1
    img = Image.open(io.BytesIO(base64.b64decode(out.png_b64)))
    # password field center [20,20,180,30] -> (110, 35) should be black
    assert img.getpixel((110, 35)) == (0, 0, 0)
