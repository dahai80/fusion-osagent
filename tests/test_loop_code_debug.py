"""F5.2 fusion-code visual-debug loop tests (offline, agent mocked)."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from os_agent.adapters.base import Screenshot
from os_agent.config import OsaConfig
from os_agent.loops.code_debug import CodeDebugLoop, VisualFeedback


class FakeExecutor:
    def __init__(self, focus_ok=True):
        self.focus_ok = focus_ok
        self.focused: list[str] = []

    async def focus_app(self, app):
        self.focused.append(app)
        if not self.focus_ok:
            raise RuntimeError("focus denied")


class FakeAgent:
    """Minimal DesktopAgent stub for the code-debug loop."""

    def __init__(self, click_ok=True, assert_ok=True, focus_ok=True):
        self.executor = FakeExecutor(focus_ok)
        self._click_ok = click_ok
        self._assert_ok = assert_ok
        self.clicks: list[str] = []

        class _AR:
            def __init__(self, ok, error=None, track="ax", meta=None):
                self.ok = ok
                self.error = error
                self.track = track
                self.meta = meta or {}

        self._AR = _AR

    async def click_by(self, query):
        self.clicks.append(query)
        if not self._click_ok:
            return self._AR(False, error="not found", track="visual")
        return self._AR(True, track="ax")

    async def assert_changed(self, before=None, expected=None):
        return self._AR(self._assert_ok, error=None if self._assert_ok else "no change", meta={"changed_ratio": 0.01 if self._assert_ok else 0.0})

    async def screenshot(self):
        return Screenshot(png_b64=base64.b64encode(b"png-bytes").decode(), width=10, height=10, scale_factor=2.0, node_tree=None)


@pytest.mark.asyncio
async def test_verify_success(tmp_path, monkeypatch):
    monkeypatch.setenv("OSA_REPORT_ROOT", str(tmp_path))
    agent = FakeAgent(click_ok=True, assert_ok=True)
    loop = CodeDebugLoop(OsaConfig(), agent)
    report = str(tmp_path / "fb.json")
    fb = await loop.verify_and_report("Notes", "click OK button", report)
    assert fb.ok is True
    assert agent.executor.focused == ["Notes"]
    assert agent.clicks == ["click OK button"]
    assert Path(report).is_file()
    data = json.loads(Path(report).read_text())
    assert data["ok"] is True
    assert data["app"] == "Notes"


@pytest.mark.asyncio
async def test_verify_click_fail_captures_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("OSA_REPORT_ROOT", str(tmp_path))
    agent = FakeAgent(click_ok=False, assert_ok=True)
    loop = CodeDebugLoop(OsaConfig(), agent)
    report = str(tmp_path / "fb.json")
    fb = await loop.verify_and_report("Notes", "click OK", report)
    assert fb.ok is False
    assert "click_by failed" in fb.reason
    assert fb.error_frame_png == b"png-bytes"
    sidecar = Path(report).with_suffix(".error.png")
    assert sidecar.is_file()


@pytest.mark.asyncio
async def test_verify_assert_fail_captures_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("OSA_REPORT_ROOT", str(tmp_path))
    agent = FakeAgent(click_ok=True, assert_ok=False)
    loop = CodeDebugLoop(OsaConfig(), agent)
    report = str(tmp_path / "fb.json")
    fb = await loop.verify_and_report("Notes", "click OK", report)
    assert fb.ok is False
    assert "assert failed" in fb.reason
    assert fb.error_frame_png is not None


@pytest.mark.asyncio
async def test_verify_focus_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("OSA_REPORT_ROOT", str(tmp_path))
    agent = FakeAgent(focus_ok=False)
    loop = CodeDebugLoop(OsaConfig(), agent)
    report = str(tmp_path / "fb.json")
    fb = await loop.verify_and_report("Notes", "click OK", report)
    assert fb.ok is False
    assert "focus_app failed" in fb.reason
    assert len(agent.clicks) == 0


@pytest.mark.asyncio
async def test_verify_rejects_path_traversal(tmp_path, monkeypatch):
    # D13: a report_path that escapes the allow-list root must be refused.
    monkeypatch.setenv("OSA_REPORT_ROOT", str(tmp_path / "safe"))
    agent = FakeAgent(click_ok=True, assert_ok=True)
    loop = CodeDebugLoop(OsaConfig(), agent)
    # absolute path outside the root
    outside = str(tmp_path / "escape" / "fb.json")
    fb = await loop.verify_and_report("Notes", "click OK", outside)
    assert fb.ok is True  # the verify still ran
    assert not Path(outside).is_file()  # but the report was NOT written


@pytest.mark.asyncio
async def test_trigger_refix_missing_bin(tmp_path):
    agent = FakeAgent()
    loop = CodeDebugLoop(OsaConfig(), agent, fusion_code_bin="/no/such/fusion-code")
    proc = loop.trigger_refix(str(tmp_path / "fb.json"))
    assert proc is None


def test_visual_feedback_to_json():
    fb = VisualFeedback(ok=True, app="X", action_query="q")
    j = fb.to_json()
    assert j["ok"] is True
    assert j["has_error_frame"] is False
