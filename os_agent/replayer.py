"""Trajectory replayer (PRD F4.3 / Phase 3).

Replay a `Script` (or a raw `Recording`) step by step through the DesktopAgent.
Before each mutating step, capture a frame; after, run the FrameAsserter
(F3.2) and mark pass/fail. A deviation (no change / semantic mismatch) is
recorded but does not abort the run — the caller decides from the report.

Semantic Guards: when a ScriptStep carries guard_kind `visual`, the replayer
re-locates `target_desc` via `agent.perception.locate` and acts on the resolved
point instead of the recorded fixed coord (F4.2 generalization). guard_kind
`none` (key presses, waits) acts directly.

No wall-clock / Math.random at module level (Rule 5).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from fusion_core import get_logger

from os_agent.adapters.base import Locator, Screenshot
from os_agent.replay_ledger import ReplayLedger

log = get_logger("os_agent.replayer")


@dataclass
class StepResult:
    seq: int
    verb: str
    ok: bool
    asserted: bool = False
    changed_ratio: float = 0.0
    error: str = ""
    guard_kind: str = "none"
    resolved_at: list[float] | None = None
    latency_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "verb": self.verb,
            "ok": self.ok,
            "asserted": self.asserted,
            "changed_ratio": self.changed_ratio,
            "error": self.error,
            "guard_kind": self.guard_kind,
            "resolved_at": self.resolved_at,
            "latency_ms": self.latency_ms,
        }


@dataclass
class ReplayReport:
    results: list[StepResult] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "passed": self.passed,
                "failed": self.failed,
                "results": [r.to_dict() for r in self.results],
                "meta": self.meta,
            },
            indent=2,
            ensure_ascii=False,
        )


class Replayer:
    """Replay a Script/Recording through DesktopAgent with frame assertion."""

    def __init__(self, agent) -> None:
        # agent: DesktopAgent — needs perception, executor, asserter, screenshot
        self.agent = agent

    async def replay_script(
        self, script, idempotency_key: str | None = None, ledger_path: str | None = None
    ) -> ReplayReport:
        """Replay a translator.Script.

        Gap 5: when `idempotency_key` is given, a ReplayLedger persists each
        completed step. A re-run with the same key skips steps already done —
        so a crashed replay resumes instead of re-executing mutating steps
        (double-click / double-type / duplicate submit). Without a key the
        replay is the original non-idempotent behavior (back-compat).
        """
        ledger = ReplayLedger(idempotency_key, ledger_path) if idempotency_key else None
        report = ReplayReport(meta=dict(getattr(script, "meta", {})))
        if ledger:
            report.meta["idempotency_key"] = idempotency_key
        for sstep in script.steps:
            # P1 fix: claim the seq atomically BEFORE executing. The old
            # is_done→execute→mark_done window let a concurrent replay with the
            # same key double-execute a mutating step (double-click/submit).
            if ledger is not None and not ledger.claim(sstep.seq):
                log.info("replay step %d: skipped (already claimed/done, idempotent resume)", sstep.seq)
                report.results.append(
                    StepResult(
                        seq=sstep.seq,
                        verb=sstep.verb,
                        ok=True,
                        guard_kind=sstep.guard_kind,
                        error="skipped: already done",
                    )
                )
                report.passed += 1
                continue
            res = await self._replay_step(sstep.seq, sstep.verb, sstep.guard_kind, sstep.target_desc, sstep.action)
            report.results.append(res)
            if res.ok:
                report.passed += 1
                if ledger is not None:
                    ledger.mark_done(res.seq)
            else:
                report.failed += 1
            log.info("replay step %d: ok=%s ratio=%.5f err=%s", res.seq, res.ok, res.changed_ratio, res.error)
        log.info("replay done: passed=%d failed=%d", report.passed, report.failed)
        return report

    async def replay_recording(
        self, recording, idempotency_key: str | None = None, ledger_path: str | None = None
    ) -> ReplayReport:
        """Replay a raw recorder.Recording (fixed coords, no guards)."""
        ledger = ReplayLedger(idempotency_key, ledger_path) if idempotency_key else None
        report = ReplayReport(meta=dict(getattr(recording, "meta", {})))
        if ledger:
            report.meta["idempotency_key"] = idempotency_key
        for step in recording.steps:
            # GA-4: claim the seq atomically BEFORE executing, mirroring
            # replay_script. The old is_done→execute→mark_done window let a
            # concurrent replay with the same key double-execute a mutating
            # fixed-coord step (double-click/submit). claim() closes it.
            if ledger is not None and not ledger.claim(step.seq):
                log.info("replay-recording step %d: skipped (already claimed/done, idempotent resume)", step.seq)
                report.results.append(
                    StepResult(seq=step.seq, verb=step.kind, ok=True, guard_kind="point", error="skipped: already done")
                )
                report.passed += 1
                continue
            action = self._action_from_step(step)
            res = await self._replay_step(step.seq, step.kind, "point", "", action)
            report.results.append(res)
            if res.ok:
                report.passed += 1
                if ledger is not None:
                    ledger.mark_done(res.seq)
            else:
                report.failed += 1
        log.info("replay-recording done: passed=%d failed=%d", report.passed, report.failed)
        return report

    async def _replay_step(
        self,
        seq: int,
        verb: str,
        guard_kind: str,
        target_desc: str,
        action: dict,
    ) -> StepResult:
        t0 = time.monotonic()
        resolved = None
        try:
            if guard_kind == "visual" and target_desc:
                pr = await self.agent.perception.locate(target_desc)
                if pr.locator.x is not None:
                    resolved = [pr.locator.x, pr.locator.y]
                    action = {**action, "at": resolved}
                else:
                    log.warning("replay step %d: guard locate failed for %r — using recorded coord", seq, target_desc)
            before = await self._capture()
            dispatch_ok = await self._dispatch(verb, action)
            after = await self._capture()
            # B9: only pass a semantic expectation when a real target description
            # exists. Using the verb ("click") as `expected` made the verifier
            # judge a meaningless string; without a desc, fall back to pixel-diff
            # only (expected=None).
            assert_ok, asserted, ratio, err = await self._assert(before, after, target_desc or None)
            ms = int((time.monotonic() - t0) * 1000)
            step_ok = dispatch_ok and (not asserted or assert_ok)
            return StepResult(
                seq=seq,
                verb=verb,
                ok=step_ok,
                asserted=asserted,
                changed_ratio=ratio,
                error=err if not step_ok else "",
                guard_kind=guard_kind,
                resolved_at=resolved,
                latency_ms=ms,
            )
        except Exception as e:
            log.error("replay step %d raised: %s", seq, e)
            return StepResult(
                seq=seq,
                verb=verb,
                ok=False,
                error=str(e),
                guard_kind=guard_kind,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

    async def _capture(self) -> Screenshot | None:
        try:
            return await self.agent.perception.capture(prefer_ax=False)
        except Exception as e:
            log.warning("replay capture failed: %s", e)
            return None

    async def _dispatch(self, verb: str, action: dict) -> bool:
        at = action.get("at")
        if verb in ("click", "double-click", "right-click"):
            if not at:
                return False
            button = {"click": "left", "double-click": "double", "right-click": "right"}.get(verb, "left")
            res = await self.agent.executor.click(Locator(kind="point", x=at[0], y=at[1]), button=button)
            return bool(res.get("ok"))
        if verb == "type":
            res = await self.agent.executor.type_text(action.get("text", ""))
            return bool(res.get("ok"))
        if verb in ("press", "key"):
            res = await self.agent.executor.key_press(action.get("key", ""), action.get("modifiers"))
            return bool(res.get("ok"))
        if verb == "scroll":
            if not at:
                return False
            res = await self.agent.executor.scroll(Locator(kind="point", x=at[0], y=at[1]), 0.0, action.get("dy", -3.0))
            return bool(res.get("ok"))
        if verb in ("drag from", "drag_start", "drag"):
            # B8: a drag is ONE atomic executor.drag(src,dst). The paired
            # "drop at"/"drag_end" step is the release half — it must NOT call
            # drag again (double-execution). Only the start half dispatches.
            # E3: accept the unified "drag" verb as well as the two half-verbs
            # so a single change to the translator VERB map cannot break one
            # path while the other keeps working.
            src = action.get("at")
            dst = action.get("drag_to")
            if not src or not dst:
                return False
            res = await self.agent.executor.drag(
                Locator(kind="point", x=src[0], y=src[1]), Locator(kind="point", x=dst[0], y=dst[1])
            )
            return bool(res.get("ok"))
        if verb in ("drop at", "drag_end"):
            # release half of the drag — already performed by the start step
            return True
        if verb == "wait":
            await self.agent.executor.wait(action.get("seconds", 0.5))
            return True
        log.warning("unknown verb in replay: %s", verb)
        return False

    async def _assert(
        self, before: Screenshot | None, after: Screenshot | None, expected: str
    ) -> tuple[bool, bool, float, str]:
        """Returns (assert_ok, asserted, changed_ratio, error)."""
        if before is None or after is None:
            return False, False, 0.0, "missing frame for assertion"
        try:
            fa = await self.agent.asserter.assert_changed(before, after, expected=expected if expected != "" else None)
            return fa.ok, True, fa.changed_ratio, fa.error or ""
        except Exception as e:
            log.warning("replay assert raised: %s", e)
            return False, False, 0.0, str(e)

    def _action_from_step(self, step) -> dict:
        if step.kind in ("click", "double_click", "right_click"):
            return {"at": step.at, "button": step.button or "left"}
        if step.kind == "type":
            return {"text": step.text}
        if step.kind == "key":
            return {"key": step.key, "modifiers": step.modifiers}
        if step.kind == "scroll":
            return {"at": step.at, "dy": -3.0}
        if step.kind in ("drag_start", "drag_end"):
            return {"at": step.at, "drag_to": step.drag_to}
        if step.kind == "wait":
            return {"seconds": 0.5}
        return {}

    @staticmethod
    def save(report: ReplayReport, path: str) -> str:
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(report.to_json())
        log.info("replay report saved: %s", path)
        return path
