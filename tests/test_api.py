"""DesktopAgent API tests — stub mode, no live siblings required."""
from __future__ import annotations

import pytest

from os_agent.api import DesktopAgent
from os_agent.config import OsaConfig


@pytest.mark.asyncio
async def test_screenshot_stub_returns_frame():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        shot = await agent.screenshot()
        assert shot.png_b64 is not None
        assert shot.width == 1440
        assert shot.scale_factor == 2.0
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_click_logs_and_returns_ok():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        res = await agent.click(100.0, 200.0)
        assert res.ok is True
        assert res.action == "click"
        assert res.latency_ms >= 0
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_click_by_uses_ax_track():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        res = await agent.click_by("OK")
        assert res.ok is True
        assert res.track in ("ax", "visual")
        assert res.meta.get("query") == "OK"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_type_and_key():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        r1 = await agent.type_text("hello")
        r2 = await agent.key("Return", modifiers=["command"])
        assert r1.ok and r2.ok
        assert r1.action == "type"
        assert r2.action == "key"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_scroll_and_drag():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        rs = await agent.scroll(100.0, 200.0, dx=0.0, dy=-10.0)
        rd = await agent.drag(10.0, 10.0, 50.0, 50.0)
        assert rs.ok and rd.ok
        assert rs.action == "scroll"
        assert rd.action == "drag"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_executor_stub_records_calls():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        await agent.click(1.0, 2.0)
        await agent.type_text("x")
        calls = agent.executor.calls
        kinds = [c.get("kind") for c in calls]
        assert "click" in kinds
        assert "type_text" in kinds
    finally:
        await agent.close()
