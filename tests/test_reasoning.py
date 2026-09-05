"""Fast/Slow dual-core reasoning tests (Phase 2.1, offline stub)."""

from __future__ import annotations

import pytest

from os_agent.adapters.base import Screenshot
from os_agent.config import OsaConfig
from os_agent.reasoning import Reasoner
from os_agent.som import SomAnnotator

SHOT = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"

# N5: Fast core skips when there is no AX tree, so give the test shot a real
# (non-sensitive) AX tree so the Fast path actually runs.
AX_TREE = '{"role":"AXWindow","children":[{"role":"AXButton","label":"OK","frame":[0,0,40,20]}]}'


class FakeMlx:
    name = "mlx-fake"

    def __init__(self, fast_resp: dict, slow_resp: dict | None = None) -> None:
        self.fast_resp = fast_resp
        self.slow_resp = slow_resp or {"action": "halt", "confidence": 0.1, "rationale": "no slow"}
        self.calls: list[tuple[str, str | None]] = []

    async def chat_json(self, prompt: str, image_b64: str, model: str | None = None) -> dict:
        self.calls.append((prompt[:40], model))
        if "Slow" in prompt or "Slow" in (prompt or ""):
            return self.slow_resp
        return self.fast_resp

    async def close(self) -> None:
        pass


def _shot() -> Screenshot:
    return Screenshot(png_b64=SHOT, width=400, height=300, scale_factor=2.0, node_tree=AX_TREE)


@pytest.mark.asyncio
async def test_fast_accepts_high_confidence():
    mlx = FakeMlx(fast_resp={"action": "click", "target": "OK", "confidence": 0.9, "unknown_dialog": False})
    r = Reasoner(OsaConfig(), mlx, SomAnnotator(OsaConfig()))
    try:
        reason = await r.decide("click OK button", _shot())
        assert reason.core == "fast"
        assert reason.action == "click"
        assert reason.escalated is False
        assert len(mlx.calls) == 1
    finally:
        await mlx.close()


@pytest.mark.asyncio
async def test_escalates_on_low_confidence():
    mlx = FakeMlx(
        fast_resp={"action": "click", "target": "maybe", "confidence": 0.2, "unknown_dialog": False},
        slow_resp={
            "action": "click",
            "target": "3",
            "confidence": 0.8,
            "rationale": "mark 3 is the OK button",
            "sub_steps": [{"action": "click", "target": "3"}],
        },
    )
    r = Reasoner(OsaConfig(), mlx, SomAnnotator(OsaConfig()))
    try:
        reason = await r.decide("click OK button", _shot())
        assert reason.core == "slow"
        assert reason.escalated is True
        assert reason.action == "click"
        assert len(reason.sub_steps) == 1
        assert len(mlx.calls) == 2
    finally:
        await mlx.close()


@pytest.mark.asyncio
async def test_escalates_on_unknown_dialog():
    mlx = FakeMlx(
        fast_resp={"action": "click", "target": "x", "confidence": 0.95, "unknown_dialog": True},
        slow_resp={"action": "click", "target": "Dismiss", "confidence": 0.7, "rationale": "dismiss dialog first"},
    )
    r = Reasoner(OsaConfig(), mlx, SomAnnotator(OsaConfig()))
    try:
        reason = await r.decide("save file", _shot())
        assert reason.core == "slow"
        assert reason.escalated is True
    finally:
        await mlx.close()


@pytest.mark.asyncio
async def test_escalates_on_failed_assertion_history():
    mlx = FakeMlx(
        fast_resp={"action": "click", "target": "OK", "confidence": 0.9, "unknown_dialog": False},
        slow_resp={"action": "click", "target": "2", "confidence": 0.6, "rationale": "relocate after assert fail"},
    )
    r = Reasoner(OsaConfig(), mlx, SomAnnotator(OsaConfig()))
    try:
        history = [{"step": "s1", "action": "click", "action_ok": True, "assert_ok": False}]
        reason = await r.decide("click OK", _shot(), history=history)
        assert reason.core == "slow"
    finally:
        await mlx.close()


@pytest.mark.asyncio
async def test_fast_error_forces_escalate():
    class ErrMlx:
        name = "mlx-err"

        async def chat_json(self, prompt, image_b64, model=None):
            raise RuntimeError("mlx down")

        async def close(self):
            pass

    r = Reasoner(OsaConfig(), ErrMlx(), SomAnnotator(OsaConfig()))
    # fast raises -> fast proposal none+unknown -> escalate -> slow also raises -> halt
    reason = await r.decide("do something", _shot())
    assert reason.core == "slow"
    assert reason.action == "halt"
    assert reason.escalated is True


@pytest.mark.asyncio
async def test_vlm_cache_skips_redundant_inference():
    # P5/B4: same query + same screenshot twice within TTL -> mlx called once.
    mlx = FakeMlx(fast_resp={"action": "click", "target": "OK", "confidence": 0.9, "unknown_dialog": False})
    r = Reasoner(OsaConfig(), mlx, SomAnnotator(OsaConfig()))
    try:
        await r.decide("click OK button", _shot())
        await r.decide("click OK button", _shot())  # identical inputs -> cache hit
        assert len(mlx.calls) == 1
        assert r.vlm_cache.hits == 1
    finally:
        await mlx.close()


@pytest.mark.asyncio
async def test_vlm_cache_miss_on_changed_screenshot():
    # different screenshot -> different image hash -> cache miss -> re-infer.
    # Use a non-1x1 image so the fail-closed blur does not collapse two distinct
    # inputs to the same blurred output (which would make the cache key collide
    # and turn a real miss into a false hit).
    import base64
    import io

    from PIL import Image

    def _colored_png(color):
        buf = io.BytesIO()
        Image.new("RGB", (200, 200), color).save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    mlx = FakeMlx(fast_resp={"action": "click", "target": "OK", "confidence": 0.9, "unknown_dialog": False})
    r = Reasoner(OsaConfig(), mlx, SomAnnotator(OsaConfig()))
    try:
        s1 = Screenshot(png_b64=_colored_png((255, 0, 0)), width=200, height=200, scale_factor=2.0, node_tree=AX_TREE)
        s2 = Screenshot(png_b64=_colored_png((0, 0, 255)), width=200, height=200, scale_factor=2.0, node_tree=AX_TREE)
        await r.decide("click OK button", s1)
        await r.decide("click OK button", s2)
        assert len(mlx.calls) == 2
    finally:
        await mlx.close()


def test_vlm_cache_disabled_when_ttl_zero():
    from os_agent.vlm_cache import VlmCache

    c = VlmCache(ttl=0.0)
    c.put("m", "p", "img", {"a": 1})
    val, hit = c.get("m", "p", "img")
    assert hit is False
    assert val is None
