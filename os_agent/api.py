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
import os
import time
from dataclasses import dataclass, field

from fusion_core import get_logger

from os_agent.action import FrameAsserter
from os_agent.adapters.agent_studio import AgentStudioAdapter, StubAgentStudioAdapter
from os_agent.adapters.base import Locator, Screenshot
from os_agent.adapters.browser import BrowserAdapter, StubBrowserAdapter
from os_agent.adapters.executor import ExecutorAdapter, StubExecutorAdapter
from os_agent.adapters.mlx import MlxAdapter, StubMlxAdapter
from os_agent.audit_log import AuditLog
from os_agent.config import OsaConfig
from os_agent.crop_zoom import CropResult, CropZoomer
from os_agent.healer import Healer
from os_agent.loops.autotest import AutotestLoop
from os_agent.loops.code_debug import CodeDebugLoop
from os_agent.mask import SensitiveMasker
from os_agent.metrics import MetricsRegistry
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
        # E5: per-agent metrics registry so a multi-node fleet can isolate
        # counters per agent instead of all sharing one module singleton.
        self.metrics = MetricsRegistry()
        # Audit gap 4: structured append-only audit trail (masked regions /
        # actions / decisions). OSA_AUDIT_PATH="" disables persistence;
        # default is in-memory only to keep offline tests side-effect-free.
        _audit_path = os.environ.get("OSA_AUDIT_PATH", "")
        self.audit = AuditLog(
            path=_audit_path or None,
            agent_id=os.environ.get("OSA_AGENT_ID", "osagent"),
            rotate_max_bytes=self.cfg.audit_rotate_max_bytes,
            retention_files=self.cfg.audit_retention_files,
            retention_days=self.cfg.audit_retention_days,
        )
        # A5: size the shared image cache from config (default was a thrashing 8).
        from os_agent import image_cache

        image_cache.configure(self.cfg.image_cache_max_entries)
        self.executor: ExecutorAdapter | StubExecutorAdapter = (
            StubExecutorAdapter(self.cfg) if self.cfg.stub_mode else ExecutorAdapter(self.cfg)
        )
        self.mlx: MlxAdapter | StubMlxAdapter = StubMlxAdapter(self.cfg) if self.cfg.stub_mode else MlxAdapter(self.cfg)
        # Gap 2: multi-node coordination. Stamp this agent's node_id onto mlx
        # (so cluster-health failures are attributed to the right node) and
        # register in the file-shared NodeRegistry so a fleet snapshot shows
        # who is live. Stub mode skips cluster wiring (no real mlx, no fleet).
        self.node_id = os.environ.get("OSA_AGENT_ID", f"osagent-{os.getpid()}")
        from os_agent.coordination import NodeRegistry, build_cluster_health

        self.cluster_health = None if self.cfg.stub_mode else build_cluster_health()
        self.registry = NodeRegistry()
        if not self.cfg.stub_mode:
            self.mlx.node_id = self.node_id
            if self.cluster_health is not None:
                self.mlx.cluster_health = self.cluster_health
            try:
                self.registry.register(self.node_id, mlx=self.mlx.model)
            except OSError as e:
                log.warning("node register failed (coordination disabled): %s", e)
                self.registry = None
        self.browser: BrowserAdapter | StubBrowserAdapter | None = (
            StubBrowserAdapter(self.cfg) if self.cfg.stub_mode else BrowserAdapter(self.cfg)
        )
        self.studio: AgentStudioAdapter | StubAgentStudioAdapter = (
            StubAgentStudioAdapter(self.cfg) if self.cfg.stub_mode else AgentStudioAdapter(self.cfg)
        )
        # E4: one shared masker for reasoner + perception (was two independent
        # instances with split masked_count + LRU caches).
        self.masker = SensitiveMasker()
        self.perception = Perception(self.cfg, self.executor, self.mlx, self.browser, masker=self.masker)
        self.som = SomAnnotator(self.cfg)
        self.asserter = FrameAsserter(self.cfg, self.mlx, masker=self.masker)
        self.healer = Healer(self.cfg, self.perception)
        self.reasoner = Reasoner(self.cfg, self.mlx, self.som, masker=self.masker, metrics=self.metrics)
        # R6: share the reasoner's vlm_cache with perception so visual locate
        # also skips re-inference on an unchanged screen.
        self.perception.vlm_cache = self.reasoner.vlm_cache
        self.crop_zoomer = CropZoomer(self.cfg)
        self.code_debug = CodeDebugLoop(self.cfg, self)
        self.autotest = AutotestLoop(self.cfg)
        self.translator = Translator()
        self.replayer = Replayer(self)
        # N9: remember the last logical-point cursor position so human-like
        # moves start from where the cursor actually is, not a teleport from
        # (0,0). A jump from the corner to the target every click is both
        # visually jarring and a bot fingerprint.
        self._cursor_pos: tuple[float, float] = (0.0, 0.0)
        log.info("DesktopAgent ready stub=%s mlx=%s", self.cfg.stub_mode, self.mlx.model)

    async def screenshot(self) -> Screenshot:
        t0 = time.monotonic()
        shot = await self.perception.capture(prefer_ax=True)
        log.info(
            "screenshot track=%s ax=%s %dms",
            "ax" if shot.has_ax else "plain",
            shot.has_ax,
            int((time.monotonic() - t0) * 1000),
        )
        return shot

    async def som_view(self) -> SomView:
        """Capture with AX tree and overlay numbered SOM marks (Phase 1)."""
        # P1 fix: mask BEFORE annotating — som.annotate draws marks onto the
        # raw png_b64, so an unmasked capture produces a marked image carrying
        # raw sensitive pixels. Any caller feeding marked_b64 to a model leaks.
        # P0 perf: mask() does PIL decode+paint+encode — offload to a thread.
        shot = await self.perception.capture(prefer_ax=True)
        masked = await asyncio.to_thread(self.masker.mask, shot)
        return await self.som.annotate(masked)

    async def click(self, x: float, y: float, button: str = "left") -> ActionResult:
        return await self._act("click", Locator(kind="point", x=x, y=y), button=button)

    async def click_by(self, query: str) -> ActionResult:
        t0 = time.monotonic()
        pr = await self.perception.locate(query)
        if pr.locator.x is None or pr.locator.y is None:
            ms = int((time.monotonic() - t0) * 1000)
            err = pr.locator.raw.get("error", "locate failed")
            log.warning("click_by: no coordinates for %r (%s)", query, err)
            return ActionResult(
                ok=False,
                action="click_by",
                track=pr.track,
                latency_ms=ms,
                error=err,
                meta={"query": query, "confidence": pr.confidence},
            )
        res = await self.executor.click(pr.locator, button="left")
        ms = int((time.monotonic() - t0) * 1000)
        return ActionResult(
            ok=res.get("ok", False),
            action="click_by",
            track=pr.track,
            latency_ms=ms,
            error=res.get("error"),
            meta={"query": query, "confidence": pr.confidence},
        )

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
        self.audit.record("assert", ok=fa.ok, changed_ratio=fa.changed_ratio, expected=expected)
        return ActionResult(
            ok=fa.ok,
            action="assert",
            latency_ms=0,
            error=fa.error,
            meta={"changed_ratio": fa.changed_ratio, "expected": expected, **fa.meta},
        )

    async def decide(self, query: str, history: list[dict] | None = None) -> Reason:
        """Fast/Slow dual-core: Fast proposes one action; escalate to Slow on low confidence / unknown dialog / failed assert."""
        # Gap 2: refresh this node's heartbeat each decide cycle so a
        # long-running agent is not reaped from the fleet registry.
        await self.heartbeat()
        shot = await self.perception.capture(prefer_ax=True)
        reason = await self.reasoner.decide(query, shot, history)
        log.info(
            "decide: core=%s action=%s conf=%.2f escalated=%s",
            reason.core,
            reason.action,
            reason.confidence,
            reason.escalated,
        )
        self.audit.record(
            "decide",
            core=reason.core,
            action=reason.action,
            confidence=reason.confidence,
            escalated=reason.escalated,
            has_ax=shot.has_ax,
            masked=shot.meta.get("masked"),
        )
        return reason

    def crop_zoom(
        self, shot: Screenshot, center_px: tuple[float, float], half_extent_px: int = 120, upscale: int = 2
    ) -> CropResult | None:
        """Patch-level crop & zoom for finer VLM grounding on dense/small controls (Phase 2.3)."""
        return self.crop_zoomer.crop_around(shot, center_px, half_extent_px=half_extent_px, upscale=upscale)

    async def click_humanlike(
        self, x: float, y: float, start: tuple[float, float] | None = None, traj: TrajectoryConfig | None = None
    ) -> ActionResult:
        """Human-like click: Bezier path to (x,y) then click (F3.1 execution, Phase 3).

        N9: when `start` is not given, begin from the last known cursor
        position (tracked across calls) instead of (0,0). A corner-to-target
        teleport every click is a bot fingerprint and wastes the path budget.
        """
        t0 = time.monotonic()
        start = start or self._cursor_pos
        path = bezier_path(start, (x, y), traj or TrajectoryConfig(seed=self.cfg.trajectory_seed))
        await self.executor.move_path(path)
        res = await self.executor.click(Locator(kind="point", x=x, y=y), button="left")
        # P3 fix: only advance the tracked cursor on success — a rejected click
        # never moved the pointer, so claiming it is at (x,y) poisons the next
        # Bezier path's origin.
        if res.get("ok"):
            self._cursor_pos = (x, y)
        ms = int((time.monotonic() - t0) * 1000)
        log.info("click_humanlike: %d waypoints %dms ok=%s", len(path), ms, res.get("ok"))
        return ActionResult(
            ok=res.get("ok", False),
            action="click_humanlike",
            latency_ms=ms,
            error=res.get("error"),
            meta={"waypoints": len(path)},
        )

    async def replay(
        self, script_or_recording, idempotency_key: str | None = None, ledger_path: str | None = None
    ) -> ReplayReport:
        """Replay a Script (F4.2) or Recording (F4.1) with per-step frame assertion (F4.3, Phase 3).

        Gap 5: `idempotency_key` enables transactional resume — a re-run with
        the same key skips steps already persisted to the ledger, so a crashed
        replay resumes instead of re-executing mutating steps. `ledger_path`
        defaults to ~/.fusion-osagent/replay/<key>.jsonl when a key is given.
        """
        if ledger_path is None and idempotency_key:
            ledger_path = os.environ.get("OSA_REPLAY_LEDGER_DIR") or os.path.join(
                os.path.expanduser("~"), ".fusion-osagent", "replay"
            )
            ledger_path = os.path.join(ledger_path, f"replay-{idempotency_key}.jsonl")
        if isinstance(script_or_recording, Script):
            report = await self.replayer.replay_script(
                script_or_recording, idempotency_key=idempotency_key, ledger_path=ledger_path
            )
        elif isinstance(script_or_recording, Recording):
            report = await self.replayer.replay_recording(
                script_or_recording, idempotency_key=idempotency_key, ledger_path=ledger_path
            )
        else:
            raise TypeError(f"replay expects Script or Recording, got {type(script_or_recording).__name__}")
        log.info("replay: passed=%d failed=%d ok=%s key=%s", report.passed, report.failed, report.ok, idempotency_key)
        self.audit.record(
            "replay", ok=report.ok, passed=report.passed, failed=report.failed, idempotency_key=idempotency_key
        )
        return report

    async def heal(self, query: str) -> ActionResult:
        """Multi-locator self-healing: AX-label → AX-role → SOM → visual."""
        hr = await self.healer.heal(query)
        log.info("heal: ok=%s strategy=%s query=%r", hr.ok, hr.strategy, query)
        self.audit.record("heal", ok=hr.ok, strategy=hr.strategy, attempts=hr.attempts)
        return ActionResult(
            ok=hr.ok,
            action="heal",
            track=hr.strategy,
            error=hr.error,
            meta={"query": query, "attempts": hr.attempts},
        )

    async def heartbeat(self) -> None:
        """Gap 2: refresh this node's heartbeat in the registry so live_nodes()
        does not reap a long-running agent. Call periodically (e.g. each decide
        cycle). Fail-open: registry unavailable = no-op."""
        if getattr(self, "registry", None) is not None:
            try:
                self.registry.heartbeat(self.node_id)
            except OSError as e:
                log.warning("heartbeat failed: %s", e)

    async def close(self) -> None:
        closes = [self.executor.close(), self.mlx.close()]
        if self.browser:
            closes.append(self.browser.close())
        closes.append(self.studio.close())
        # Bound the shutdown so a hung adapter close (stuck UDS recv / mlx
        # aclose) cannot hang the SIGTERM graceful-close path forever.
        try:
            results = await asyncio.wait_for(asyncio.gather(*closes, return_exceptions=True), timeout=10.0)
        except TimeoutError:
            log.warning("close timed out after 10s — some adapters may not have closed cleanly")
            results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                log.warning("close[%d] raised: %s", i, r)
        # Gap 2: deregister so the fleet snapshot no longer lists this node.
        if getattr(self, "registry", None) is not None:
            try:
                self.registry.deregister(self.node_id)
            except OSError as e:
                log.warning("deregister failed: %s", e)
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
        self.metrics.inc(f"action.{action}.total")
        self.metrics.inc(f"action.{action}.ok" if res.get("ok") else f"action.{action}.fail")
        self.metrics.observe(f"action.{action}.latency_ms", ms)
        self.audit.record("action", action_kind=action, ok=bool(res.get("ok")), latency_ms=ms, error=res.get("error"))
        return ActionResult(ok=res.get("ok", False), action=action, latency_ms=ms, error=res.get("error"))

    async def _act_raw(self, action: str, fn) -> ActionResult:
        t0 = time.monotonic()
        res = await fn()
        ms = int((time.monotonic() - t0) * 1000)
        self.metrics.inc(f"action.{action}.total")
        self.metrics.inc(f"action.{action}.ok" if res.get("ok") else f"action.{action}.fail")
        self.metrics.observe(f"action.{action}.latency_ms", ms)
        self.audit.record("action", action_kind=action, ok=bool(res.get("ok")), latency_ms=ms, error=res.get("error"))
        return ActionResult(ok=res.get("ok", False), action=action, latency_ms=ms, error=res.get("error"))

    async def health(self) -> dict:
        """Aggregate health of mlx + executor + browser. P0 fix: the CLI
        `health` command only pinged mlx, so a down executor/browser socket
        reported green. Returns a per-component dict + overall ok.
        """
        components = {"mlx": self.mlx.health()}
        if hasattr(self.executor, "health"):
            components["executor"] = self.executor.health()
        if self.browser is not None and hasattr(self.browser, "health"):
            components["browser"] = self.browser.health()
        results = {}
        for name, coro in components.items():
            try:
                results[name] = await coro
            except Exception as e:
                log.warning("health %s raised: %s", name, e)
                results[name] = False
        results["ok"] = all(results.values())
        return results

    def metrics_snapshot(self) -> dict:
        """Full production-view metrics in one call.

        An external Prometheus exporter or fusion-core monitor scrapes this to
        observe the agent without coupling to internal classes. Includes the
        masker's masked-region count + the reasoner vlm cache stats so the
        snapshot is a complete production-view in one call.
        """
        snap = self.metrics.snapshot()
        snap["masker_masked_total"] = getattr(self.masker, "masked_count", 0)
        snap["vlm_cache"] = self.reasoner.vlm_cache.stats()
        # Gap 3/2: surface the circuit breaker + cluster-health state so an
        # operator scraping metrics can see WHY every click_by fast-fails
        # (breaker OPEN / cluster OPEN) without reading log lines.
        breaker = getattr(self.mlx, "breaker", None)
        if breaker is not None:
            snap["breaker"] = breaker.snapshot()
        cluster = getattr(self, "cluster_health", None)
        if cluster is not None:
            try:
                snap["cluster_health"] = cluster.snapshot()
            except OSError as e:
                log.warning("cluster snapshot failed: %s", e)
                snap["cluster_health"] = {"error": str(e)}
        snap["coordination_enabled"] = getattr(self, "registry", None) is not None
        # P3 perf: this runs on every scrape; demote the full-digest log to
        # debug so a high-frequency exporter does not flood the INFO stream.
        log.debug("metrics snapshot: %s", {k: v for k, v in snap.items() if k != "histograms"})
        return snap
