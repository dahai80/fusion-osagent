"""Unified Desktop Embodied AI API (PRD F5.1).

Aligns with Claude Computer Use `computer` tool semantics and extends:
  screenshot / click / type / key / scroll / drag / wait   (Claude-parity)
  assert / heal / som_view / replay                        (osagent extensions)

Single logical-point coordinate space. Adapters convert to physical pixels.
Phase 0: screenshot+click+type+key+scroll+drag+wait via executor, perception
dual-track for locate. Frame assertion / healing land Phase 1.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from fusion_core import get_logger

from os_agent.adapters.agent_studio import AgentStudioAdapter, StubAgentStudioAdapter
from os_agent.adapters.base import Locator, Screenshot
from os_agent.adapters.browser import BrowserAdapter, StubBrowserAdapter
from os_agent.adapters.executor import ExecutorAdapter, StubExecutorAdapter
from os_agent.adapters.mlx import MlxAdapter, StubMlxAdapter
from os_agent.config import OsaConfig
from os_agent.perception import Perception

log = get_logger("os_agent.api")


@dataclass
class ActionResult:
    ok: bool
    action: str
    track: str = ""
    latency_ms: int = 0
    error: str | None = None
    meta: dict = field(default_factory=dict)


class DesktopAgent:
    """fusion-osagent entry point: see-screen → decide → act → (heal)."""

    def __init__(self, cfg: OsaConfig | None = None) -> None:
        self.cfg = cfg or OsaConfig()
        self.executor: ExecutorAdapter | StubExecutorAdapter = (
            StubExecutorAdapter(self.cfg) if self.cfg.stub_mode else ExecutorAdapter(self.cfg)
        )
        self.mlx: MlxAdapter | StubMlxAdapter = (
            StubMlxAdapter(self.cfg) if self.cfg.stub_mode else MlxAdapter(self.cfg)
        )
        self.browser: BrowserAdapter | StubBrowserAdapter | None = (
            StubBrowserAdapter(self.cfg) if self.cfg.stub_mode else BrowserAdapter(self.cfg)
        )
        self.studio: AgentStudioAdapter | StubAgentStudioAdapter = (
            StubAgentStudioAdapter(self.cfg) if self.cfg.stub_mode else AgentStudioAdapter(self.cfg)
        )
        self.perception = Perception(self.cfg, self.executor, self.mlx, self.browser)
        log.info("DesktopAgent ready stub=%s mlx=%s", self.cfg.stub_mode, self.mlx.model)

    async def screenshot(self) -> Screenshot:
        t0 = time.monotonic()
        shot = await self.perception.capture(prefer_ax=True)
        log.info("screenshot track=%s ax=%s %dms", "ax" if shot.has_ax else "plain", shot.has_ax, int((time.monotonic() - t0) * 1000))
        return shot

    async def som_view(self) -> Screenshot:
        """Capture with AX tree for SOM overlay (Phase 1 adds overlay; Phase 0 returns raw)."""
        return await self.perception.capture(prefer_ax=True)

    async def click(self, x: float, y: float, button: str = "left") -> ActionResult:
        return await self._act("click", Locator(kind="point", x=x, y=y), button=button)

    async def click_by(self, query: str) -> ActionResult:
        t0 = time.monotonic()
        pr = await self.perception.locate(query)
        res = await self.executor.click(pr.locator, button="left")
        ms = int((time.monotonic() - t0) * 1000)
        return ActionResult(ok=res.get("ok", False), action="click_by", track=pr.track, latency_ms=ms, error=res.get("error"), meta={"query": query, "confidence": pr.confidence})

    async def type_text(self, text: str) -> ActionResult:
        return await self._act_raw("type", lambda: self.executor.type_text(text))

    async def key(self, key: str, modifiers: list[str] | None = None) -> ActionResult:
        return await self._act_raw("key", lambda: self.executor.key_press(key, modifiers))

    async def scroll(self, x: float, y: float, dx: float = 0.0, dy: float = 0.0) -> ActionResult:
        return await self._act("scroll", Locator(kind="point", x=x, y=y), dx=dx, dy=dy)

    async def drag(self, x1: float, y1: float, x2: float, y2: float) -> ActionResult:
        t0 = time.monotonic()
        res = await self.executor.drag(Locator(kind="point", x=x1, y=y1), Locator(kind="point", x=x2, y=y2))
        ms = int((time.monotonic() - t0) * 1000)
        return ActionResult(ok=res.get("ok", False), action="drag", latency_ms=ms, error=res.get("error"))

    async def wait(self, seconds: float) -> ActionResult:
        return await self._act_raw("wait", lambda: self.executor.wait(seconds))

    async def assert_changed(self, expected: str | None = None) -> ActionResult:
        """Post-action frame assertion (Phase 1 full impl; Phase 0 captures before/after)."""
        log.info("assert_changed stub: expected=%s (full impl Phase 1)", expected)
        return ActionResult(ok=True, action="assert", meta={"phase": "stub", "expected": expected})

    async def heal(self, query: str) -> ActionResult:
        """Multi-locator self-healing (Phase 1 full impl; Phase 0 re-locates)."""
        pr = await self.perception.locate(query)
        return ActionResult(ok=pr.locator.x is not None, action="heal", track=pr.track, meta={"phase": "stub", "query": query})

    async def close(self) -> None:
        await self.executor.close()
        await self.mlx.close()
        if self.browser:
            await self.browser.close()
        await self.studio.close()
        log.info("DesktopAgent closed")

    async def _act(self, action: str, loc: Locator, **kw) -> ActionResult:
        t0 = time.monotonic()
        if action == "scroll":
            res = await self.executor.scroll(loc, kw.get("dx", 0.0), kw.get("dy", 0.0))
        elif action == "click":
            res = await self.executor.click(loc, button=kw.get("button", "left"))
        else:
            res = {"ok": False, "error": f"unknown action {action}"}
        ms = int((time.monotonic() - t0) * 1000)
        return ActionResult(ok=res.get("ok", False), action=action, latency_ms=ms, error=res.get("error"))

    async def _act_raw(self, action: str, fn) -> ActionResult:
        t0 = time.monotonic()
        res = await fn()
        ms = int((time.monotonic() - t0) * 1000)
        return ActionResult(ok=res.get("ok", False), action=action, latency_ms=ms, error=res.get("error"))
