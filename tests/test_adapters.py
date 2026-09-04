"""Adapter contract tests — stub implementations satisfy the Adapter protocol."""
from __future__ import annotations

import pytest

from os_agent.adapters.agent_studio import StubAgentStudioAdapter
from os_agent.adapters.base import Adapter, Locator, Screenshot
from os_agent.adapters.browser import StubBrowserAdapter
from os_agent.adapters.executor import StubExecutorAdapter
from os_agent.adapters.mlx import StubMlxAdapter
from os_agent.config import OsaConfig


@pytest.mark.asyncio
async def test_executor_stub_screenshot():
    cfg = OsaConfig(stub_mode=True)
    ex = StubExecutorAdapter(cfg)
    shot = await ex.screenshot()
    assert isinstance(shot, Screenshot)
    assert shot.png_b64 is not None
    await ex.close()


@pytest.mark.asyncio
async def test_executor_stub_click_pixel_conversion():
    cfg = OsaConfig(stub_mode=True)
    ex = StubExecutorAdapter(cfg)
    res = await ex.click(Locator(kind="point", x=50.0, y=60.0))
    assert res["ok"] is True
    last = ex.calls[-1]
    assert last["at"] == [100.0, 120.0]  # 2x scale


@pytest.mark.asyncio
async def test_mlx_stub_returns_click_json():
    cfg = OsaConfig(stub_mode=True)
    mlx = StubMlxAdapter(cfg)
    data = await mlx.chat_json("click the OK button", "img")
    assert data.get("action") == "click"
    assert "x" in data and "y" in data


@pytest.mark.asyncio
async def test_browser_stub_navigate_and_screenshot():
    cfg = OsaConfig(stub_mode=True)
    br = StubBrowserAdapter(cfg)
    r = await br.navigate("https://example.com")
    assert r["ok"] is True
    shot = await br.screenshot()
    assert shot.node_tree is not None
    await br.close()


@pytest.mark.asyncio
async def test_agent_studio_stub_list_tools():
    cfg = OsaConfig(stub_mode=True)
    st = StubAgentStudioAdapter(cfg)
    tools = await st.list_tools()
    assert len(tools) >= 4
    names = {t["name"] for t in tools}
    assert {"screen_capture", "mouse", "keyboard", "clipboard"} <= names


def test_adapters_satisfy_protocol():
    cfg = OsaConfig(stub_mode=True)
    assert isinstance(StubExecutorAdapter(cfg), Adapter)
    assert isinstance(StubBrowserAdapter(cfg), Adapter)
