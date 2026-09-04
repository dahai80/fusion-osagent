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

from os_agent.action import FrameAsserter
from os_agent.adapters.agent_studio import AgentStudioAdapter, StubAgentStudioAdapter
from os_agent.adapters.base import Locator, Screenshot
from os_agent.adapters.browser import BrowserAdapter, StubBrowserAdapter
from os_agent.adapters.executor import ExecutorAdapter, StubExecutorAdapter
from os_agent.adapters.mlx import MlxAdapter, StubMlxAdapter
from os_agent.config import OsaConfig
from os_agent.crop_zoom import CropResult, CropZoomer
from os_agent.healer import Healer
from os_agent.perception import Perception
from os_agent.reasoning import Reason, Reasoner
from os_agent.som import SomAnnotator, SomView

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
        self.som = SomAnnotator(self.cfg)
        self.asserter = FrameAsserter(self.cfg, self.mlx)
        self.healer = Healer(self.cfg, self.perception)
        self.reasoner = Reasoner(self.cfg, self.mlx, self.som)
        self.crop_zoomer = CropZoomer(self.cfg)
        log.info("DesktopAgent ready stub=%s mlx=%s", self.cfg.stub_mode, self.mlx.model)

    async def screenshot(self) -> Screenshot:
        t0 = time.monotonic()
        shot = await self.perception.capture(prefer_ax=True)
        log.info("screenshot track=%s ax=%s %dms", "ax" if shot.has_ax else "plain", shot.has_ax, int((time.monotonic() - t0) * 1000))
        return shot

    async def som_view(self) -> SomView:
        """Capture with AX tree and overlay numbered SOM marks (Phase 1)."""
        shot = await self.perception.capture(prefer_ax=True)
        return await self.som.annotate(shot)

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

    async def assert_changed(self, before: Screenshot | None = None, expected: str | None = None) -> ActionResult:
        """Post-action frame assertion: diff before/after, optional VLM semantic verify."""
        before = before or await self.perception.capture(prefer_ax=False)
        after = await self.perception.capture(prefer_ax=False)
        fa = await self.asserter.assert_changed(before, after, expected=expected)
        log.info("assert_changed: ok=%s ratio=%.5f err=%s", fa.ok, fa.changed_ratio, fa.error)
        return ActionResult(
            ok=fa.ok,
            action="assert",
            latency_ms=0,
            error=fa.error,
            meta={"changed_ratio": fa.changed_ratio, "expected": expected, **fa.meta},
        )

    async def decide(self, query: str, history: list[dict] | None = None) -> Reason:
        """Fast/Slow dual-core: Fast proposes one action; escalate to Slow on low confidence / unknown dialog / failed assert."""
        shot = await self.perception.capture(prefer_ax=True)
        reason = await self.reasoner.decide(query, shot, history)
        log.info("decide: core=%s action=%s conf=%.2f escalated=%s", reason.core, reason.action, reason.confidence, reason.escalated)
        return reason

    def crop_zoom(self, shot: Screenshot, center_px: tuple[float, float], half_extent_px: int = 120, upscale: int = 2) -> CropResult | None:
        """Patch-level crop & zoom for finer VLM grounding on dense/small controls (Phase 2.3)."""
        return self.crop_zoomer.crop_around(shot, center_px, half_extent_px=half_extent_px, upscale=upscale)

    async def heal(self, query: str) -> ActionResult:
        """Multi-locator self-healing: AX-label → AX-role → SOM → visual."""
        hr = await self.healer.heal(query)
        log.info("heal: ok=%s strategy=%s query=%r", hr.ok, hr.strategy, query)
        return ActionResult(
            ok=hr.ok,
            action="heal",
            track=hr.strategy,
            error=hr.error,
            meta={"query": query, "attempts": hr.attempts},
        )

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
