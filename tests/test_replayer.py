from __future__ import annotations

import os
import tempfile

from os_agent.action import FrameAssertion
from os_agent.adapters.base import Locator, Screenshot
from os_agent.recorder import ManualEventSource, Recorder
from os_agent.replayer import Replayer, ReplayReport
from os_agent.translator import Translator


class _FakeAsserter:
    def __init__(self, ok=True, ratio=0.05):
        self._ok = ok
        self._ratio = ratio

    async def assert_changed(self, before, after, expected=None, threshold=0.0):
        return FrameAssertion(
            ok=self._ok, changed=self._ok, changed_ratio=self._ratio, error="" if self._ok else "no change"
        )


class _FakeExecutor:
    def __init__(self):
        self.calls = []

    async def click(self, loc, button="left"):
        self.calls.append(("click", loc.as_point(), button))
        return {"ok": True, "error": None}

    async def type_text(self, text):
        self.calls.append(("type", text))
        return {"ok": True, "error": None}

    async def key_press(self, key, modifiers=None):
        self.calls.append(("key", key, modifiers or []))
        return {"ok": True, "error": None}

    async def scroll(self, loc, dx=0.0, dy=0.0):
        self.calls.append(("scroll", loc.as_point(), dy))
        return {"ok": True, "error": None}

    async def drag(self, src, dst):
        self.calls.append(("drag", src.as_point(), dst.as_point()))
        return {"ok": True, "error": None}

    async def wait(self, seconds):
        self.calls.append(("wait", seconds))
        return {"ok": True, "error": None}


class _FakePerception:
    def __init__(self, loc=(50.0, 60.0)):
        self._loc = loc

    async def capture(self, prefer_ax=False):
        return Screenshot(png_b64="iVBORw0KGgo=", width=100, height=100, scale_factor=2.0, node_tree=None)

    async def locate(self, query):
        return type(
            "PR",
            (),
            {"locator": Locator(kind="visual", x=self._loc[0], y=self._loc[1])},
        )()


class _FakeAgent:
    def __init__(self, assert_ok=True):
        self.executor = _FakeExecutor()
        self.perception = _FakePerception()
        self.asserter = _FakeAsserter(ok=assert_ok)


def _rec(events):
    return Recorder(ManualEventSource(events), capture=lambda: None, clock=lambda: 0.0).record()


async def test_replay_recording_click_passes():
    rec = _rec([{"kind": "click", "at": [10.0, 20.0], "button": "left"}])
    agent = _FakeAgent(assert_ok=True)
    rep = Replayer(agent)
    report = await rep.replay_recording(rec)
    assert report.passed == 1
    assert report.failed == 0
    assert report.results[0].verb == "click"
    assert agent.executor.calls[0][0] == "click"


async def test_replay_script_visual_guard_relocates():
    # D11: visual guard + re-location only happens when a describer produced a
    # real element description. Use a describer so the guard is "visual" and the
    # replayer re-locates via the fake perception point (50,60), not 999.
    rec = _rec([{"kind": "click", "at": [999.0, 999.0], "button": "left"}])
    rec.steps[0].screenshot_b64 = "PNG"
    script = Translator(describer=lambda b64: "the target button").translate(rec)
    agent = _FakeAgent(assert_ok=True)
    rep = Replayer(agent)
    report = await rep.replay_script(script)
    assert report.results[0].guard_kind == "visual"
    assert report.results[0].resolved_at == [50.0, 60.0]
    assert agent.executor.calls[0][1] == (50.0, 60.0)


async def test_replay_marks_failed_when_assertion_fails():
    rec = _rec(
        [
            {"kind": "click", "at": [1.0, 2.0]},
            {"kind": "type", "text": "hi"},
        ]
    )
    agent = _FakeAgent(assert_ok=False)
    rep = Replayer(agent)
    report = await rep.replay_recording(rec)
    assert report.failed == 2
    assert report.ok is False


async def test_replay_key_and_wait_dispatch():
    rec = _rec(
        [
            {"kind": "key", "key": "Return", "modifiers": ["command"]},
            {"kind": "wait"},
        ]
    )
    agent = _FakeAgent(assert_ok=True)
    rep = Replayer(agent)
    report = await rep.replay_recording(rec)
    assert report.passed == 2
    kinds = [c[0] for c in agent.executor.calls]
    assert "key" in kinds
    assert "wait" in kinds


def test_replay_report_save():
    rep = ReplayReport()
    d = tempfile.mkdtemp()
    path = os.path.join(d, "r.json")
    Replayer.save(rep, path)
    assert os.path.exists(path)
    os.remove(path)
    os.rmdir(d)
