"""Healer tests — degrade chain: AX-label → ax-role → visual."""

from __future__ import annotations

import pytest

from os_agent.adapters.base import Screenshot
from os_agent.adapters.executor import StubExecutorAdapter
from os_agent.adapters.mlx import StubMlxAdapter
from os_agent.config import OsaConfig
from os_agent.healer import Healer
from os_agent.perception import Perception

SHOT_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"


def _shot(tree: str | None) -> Screenshot:
    return Screenshot(png_b64=SHOT_B64, width=1440, height=900, scale_factor=2.0, node_tree=tree)


TREE_LABEL_HIT = '{"role":"AXWindow","children":[{"role":"AXButton","label":"OK","frame":[100,100,40,20]}]}'
TREE_ROLE_HIT = '{"role":"AXWindow","children":[{"role":"AXButton","label":"Confirm Submit","frame":[200,200,60,24]}]}'


@pytest.mark.asyncio
async def test_heal_ax_label_exact_match():
    cfg = OsaConfig(stub_mode=True)
    p = Perception(cfg, StubExecutorAdapter(cfg), StubMlxAdapter(cfg))
    h = Healer(cfg, p)
    hr = await h.heal("OK")
    assert hr.ok is True
    assert hr.strategy == "ax-label"


@pytest.mark.asyncio
async def test_heal_ax_role_partial_label():
    cfg = OsaConfig(stub_mode=True)
    p = Perception(cfg, StubExecutorAdapter(cfg, tree=TREE_ROLE_HIT), StubMlxAdapter(cfg))
    h = Healer(cfg, p)
    hr = await h.heal("Submit")
    assert hr.ok is True
    assert hr.strategy == "ax-role"


@pytest.mark.asyncio
async def test_heal_visual_fallback():
    cfg = OsaConfig(stub_mode=True)
    p = Perception(cfg, StubExecutorAdapter(cfg), StubMlxAdapter(cfg))
    h = Healer(cfg, p)
    hr = await h.heal("nonexistent-xyz")
    assert hr.ok is True
    assert hr.strategy == "visual"


@pytest.mark.asyncio
async def test_heal_records_all_attempts():
    cfg = OsaConfig(stub_mode=True)
    p = Perception(cfg, StubExecutorAdapter(cfg), StubMlxAdapter(cfg))
    h = Healer(cfg, p)
    hr = await h.heal("nonexistent-xyz")
    strategies = [a["strategy"] for a in hr.attempts]
    assert strategies == ["ax-label", "ax-role", "visual"]
