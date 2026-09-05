"""F5.2 fusion-code visual-debug loop — patch → verify → screenshot → feed back.

After fusion-code patches code, osagent launches the app/preview, drives the
UI to verify the fix, captures an error screenshot if verification fails, and
writes a structured VisualFeedback JSON that fusion-code consumes to auto-fix.

The feedback JSON contract is local to osagent for now; the cross-repo stable
format is pending upstream issue C1 (fusion-code visual-feedback protocol).
osagent does not patch fusion-code — it only emits the report file + optionally
invokes the `fusion-code` CLI to trigger a re-fix cycle.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from fusion_core import get_logger

from os_agent.config import OsaConfig

log = get_logger("os_agent.loops.code_debug")


def _report_root() -> Path:
    # D13: report files are written under an allow-list root so a
    # malicious/crafted report_path (LLM output is untrusted) cannot overwrite
    # arbitrary files or write a sidecar PNG anywhere on disk. The root is
    # overridable via OSA_REPORT_ROOT (for tests / sandboxed runs).
    env_root = os.environ.get("OSA_REPORT_ROOT")
    root = Path(env_root) if env_root else Path.home() / ".fusion-osagent" / "reports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_report_path(report_path: str) -> Path:
    """Resolve report_path under the report root, refusing path traversal."""
    root = _report_root()
    p = Path(report_path)
    if not p.is_absolute():
        p = root / p
    try:
        resolved = p.resolve()
        resolved.relative_to(root.resolve())
    except ValueError as e:
        log.error("code_debug report_path escapes allow-list root: %s", report_path)
        raise ValueError(f"report_path escapes allow-list: {report_path}") from e
    return resolved


@dataclass
class VisualFeedback:
    ok: bool
    app: str
    action_query: str
    error_frame_png: bytes | None = None
    reason: str = ""
    defects: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "app": self.app,
            "action_query": self.action_query,
            "reason": self.reason,
            "defects": self.defects,
            "has_error_frame": self.error_frame_png is not None,
            **self.raw,
        }


class CodeDebugLoop:
    """Orchestrate fusion-code patch → osagent verify → feedback report."""

    def __init__(self, cfg: OsaConfig, agent, fusion_code_bin: str = "fusion-code") -> None:
        self.cfg = cfg
        self.agent = agent  # DesktopAgent — drives the verify UI
        self.fusion_code_bin = fusion_code_bin

    async def verify_and_report(self, app: str, action_query: str, report_path: str) -> VisualFeedback:
        """Launch app, drive `action_query`, capture frame on failure, write report.

        Flow: focus_app → click_by(action_query) → assert_changed. If the action
        or assertion fails, capture the current frame as evidence. Write the
        VisualFeedback JSON to `report_path` regardless (so fusion-code always
        gets a verdict). Returns the feedback for in-process callers too.
        """
        fb = VisualFeedback(ok=False, app=app, action_query=action_query)
        try:
            await self.agent.executor.focus_app(app)
        except Exception as e:
            fb.reason = f"focus_app failed: {e}"
            log.error("code_debug focus failed: %s", e)
            self.write_report(fb, report_path)
            return fb
        # P1 arch fix: capture `before` BEFORE the click so assert_changed
        # compares a true pre-action frame against its own post-action `after`.
        # The old code captured `before` after click_by, so before+after were
        # both post-click and assert_changed always reported "no change".
        try:
            before = await self.agent.screenshot()
        except Exception as e:
            fb.reason = f"pre-click capture failed: {e}"
            log.error("code_debug before-capture failed: %s", e)
            self.write_report(fb, report_path)
            return fb
        try:
            res = await self.agent.click_by(action_query)
            fb.raw["click_ok"] = res.ok
            fb.raw["click_track"] = res.track
            if not res.ok:
                fb.reason = f"click_by failed: {res.error}"
                fb.error_frame_png = await self._capture_failure(fb.reason)
                self.write_report(fb, report_path)
                return fb
        except Exception as e:
            fb.reason = f"click_by raised: {e}"
            fb.error_frame_png = await self._capture_failure(fb.reason)
            self.write_report(fb, report_path)
            return fb
        try:
            # B18: do not pass the action query ("click submit button") as the
            # semantic `expected` — that asks the VLM whether the screenshot
            # "matches the action", which is meaningless. Without a real
            # expected-outcome string, fall back to pixel-diff verification
            # only (expected=None) so we assert "something changed", not a
            # nonsensical semantic match.
            assertion = await self.agent.assert_changed(before=before, expected=None)
            fb.raw["assert_ok"] = assertion.ok
            fb.raw["changed_ratio"] = assertion.meta.get("changed_ratio")
            if assertion.ok:
                fb.ok = True
                fb.reason = "verified: UI changed as expected"
            else:
                fb.reason = f"assert failed: {assertion.error}"
                fb.error_frame_png = await self._capture_failure(fb.reason)
        except Exception as e:
            fb.reason = f"assert raised: {e}"
            fb.error_frame_png = await self._capture_failure(fb.reason)
        self.write_report(fb, report_path)
        log.info("code_debug verify: ok=%s reason=%s report=%s", fb.ok, fb.reason, report_path)
        return fb

    async def _capture_failure(self, reason: str) -> bytes | None:
        try:
            shot = await self.agent.screenshot()
            if shot.png_b64:
                return base64.b64decode(shot.png_b64)
        except Exception as e:
            log.error("code_debug capture failed: %s", e)
        return None

    def write_report(self, fb: VisualFeedback, report_path: str) -> str:
        try:
            safe = _safe_report_path(report_path)
        except ValueError:
            # path failed the allow-list check: report loudly but do not crash
            # the whole verify loop; the feedback object still carries the verdict.
            log.error("write_report rejected unsafe path %s — report not written", report_path)
            return report_path
        safe.parent.mkdir(parents=True, exist_ok=True)
        payload = fb.to_json()
        if fb.error_frame_png:
            sidecar = str(safe.with_suffix(".error.png"))
            Path(sidecar).write_bytes(fb.error_frame_png)
            payload["error_frame"] = sidecar
        safe.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        return str(safe)

    def trigger_refix(self, report_path: str) -> subprocess.CompletedProcess | None:
        """Synchronous fusion-code CLI re-fix (legacy entry point).

        A3: prefer `trigger_refix_async` from async contexts — this synchronous
        `subprocess.run(timeout=300)` blocks the event loop for up to 5 minutes
        if called from a coroutine, freezing every concurrent screenshot/click.
        Kept for non-async callers (CLI scripts, notebooks).
        """
        cmd = [self.fusion_code_bin, "--visual-feedback", report_path]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            log.info("fusion-code refix exit=%d", proc.returncode)
            return proc
        except FileNotFoundError:
            log.warning("fusion-code bin missing — report left at %s", report_path)
            return None
        except subprocess.TimeoutExpired:
            log.error("fusion-code refix timed out")
            return None

    async def trigger_refix_async(self, report_path: str) -> subprocess.CompletedProcess | None:
        """Async fusion-code CLI re-fix — offloads subprocess.run to a thread.

        A3: `subprocess.run(timeout=300)` is blocking; calling the sync
        `trigger_fix` from the Agent event loop froze every concurrent task for
        up to 5 minutes (model load). asyncio.to_thread lets the loop keep
        running while the subprocess blocks in a worker thread.
        """
        import asyncio

        return await asyncio.to_thread(self.trigger_refix, report_path)
