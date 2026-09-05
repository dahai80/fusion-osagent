"""Unified Desktop Embodied AI API (PRD F5.1).

Aligns with Claude Computer Use `computer` tool semantics and extends:
  screenshot / click / type / key / scroll / drag / wait   (Claude-parity)
  assert / heal / som_view / replay                        (osagent extensions)

Single logical-point coordinate space. Adapters convert to physical pixels.
Phase 0: screenshot+click+type+key+scroll+drag+wait via executor, perception
dual-track for locate. Frame assertion / healing land Phase 1.
"""
from __future__ import annotations

import asyncio
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
from os_agent.loops.autotest import AutotestLoop
from os_agent.loops.code_debug import CodeDebugLoop
from os_agent.perception import Perception
from os_agent.reasoning import Reason, Reasoner
from os_agent.recorder import Recording
from os_agent.replayer import Replayer, ReplayReport
from os_agent.som import SomAnnotator, SomView
from os_agent.trajectory import TrajectoryConfig, bezier_path
from os_agent.translator import Script, Translator

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
        self.code_debug = CodeDebugLoop(self.cfg, self)
        self.autotest = AutotestLoop(self.cfg)
        self.translator = Translator()
        self.replayer = Replayer(self)
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
        if pr.locator.x is None or pr.locator.y is None:
            ms = int((time.monotonic() - t0) * 1000)
            err = pr.locator.raw.get("error", "locate failed")
            log.warning("click_by: no coordinates for %r (%s)", query, err)
            return ActionResult(ok=False, action="click_by", track=pr.track, latency_ms=ms, error=err, meta={"query": query, "confidence": pr.confidence})
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
        """Post-action frame assertion: diff before/after, optional VLM semantic verify.

        A3: `before` is mandatory. The old `before = before or capture()` fallback
        captured before+after back-to-back with no action between them, so the
        pixel diff was always 0 and the assertion always reported "no change"
        — a constant false negative that broke self-heal/replay decisions for
        any caller that did not pass an explicit before frame.
        """
        if before is None:
            log.error("assert_changed: missing `before` frame — refusing back-to-back capture")
            return ActionResult(
                ok=False,
                action="assert",
                error="assert_changed requires an explicit `before` frame captured before the action",
            )
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

    async def click_humanlike(self, x: float, y: float, start: tuple[float, float] | None = None, traj: TrajectoryConfig | None = None) -> ActionResult:
        """Human-like click: Bezier path to (x,y) then click (F3.1 execution, Phase 3)."""
        t0 = time.monotonic()
        start = start or (0.0, 0.0)
        path = bezier_path(start, (x, y), traj or TrajectoryConfig(seed=self.cfg.trajectory_seed))
        await self.executor.move_path(path)
        res = await self.executor.click(Locator(kind="point", x=x, y=y), button="left")
        ms = int((time.monotonic() - t0) * 1000)
        log.info("click_humanlike: %d waypoints %dms ok=%s", len(path), ms, res.get("ok"))
        return ActionResult(ok=res.get("ok", False), action="click_humanlike", latency_ms=ms, error=res.get("error"), meta={"waypoints": len(path)})

    async def replay(self, script_or_recording) -> ReplayReport:
        """Replay a Script (F4.2) or Recording (F4.1) with per-step frame assertion (F4.3, Phase 3)."""
        if isinstance(script_or_recording, Script):
            report = await self.replayer.replay_script(script_or_recording)
        elif isinstance(script_or_recording, Recording):
            report = await self.replayer.replay_recording(script_or_recording)
        else:
            raise TypeError(f"replay expects Script or Recording, got {type(script_or_recording).__name__}")
        log.info("replay: passed=%d failed=%d ok=%s", report.passed, report.failed, report.ok)
        return report

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
        closes = [self.executor.close(), self.mlx.close()]
        if self.browser:
            closes.append(self.browser.close())
        closes.append(self.studio.close())
        results = await asyncio.gather(*closes, return_exceptions=True)
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                log.warning("close[%d] raised: %s", i, r)
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
