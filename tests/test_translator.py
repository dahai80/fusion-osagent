from __future__ import annotations

import os
import tempfile

from os_agent.recorder import ManualEventSource, Recorder
from os_agent.translator import Translator


def _rec(events):
    return Recorder(ManualEventSource(events), capture=lambda: None, clock=lambda: 0.0).record()


def test_click_translates_to_point_guard_without_describer():
    # D11: without a describer there is no element description to re-locate by,
    # so the guard degrades to "point" (replay the recorded coord) rather than
    # claiming a visual guard that can never be satisfied.
    rec = _rec([{"kind": "click", "at": [100.0, 200.0], "button": "left"}])
    script = Translator().translate(rec)
    assert len(script.steps) == 1
    s = script.steps[0]
    assert s.verb == "click"
    assert s.guard_kind == "point"
    assert s.action["at"] == [100.0, 200.0]
    assert s.action["button"] == "left"


def test_click_translates_to_visual_guard_with_describer():
    rec = _rec([{"kind": "click", "at": [100.0, 200.0], "button": "left"}])
    rec.steps[0].screenshot_b64 = "PNG"
    script = Translator(describer=lambda b64: "the Submit button").translate(rec)
    s = script.steps[0]
    assert s.guard_kind == "visual"
    assert s.target_desc == "the Submit button"


def test_key_translates_to_none_guard_with_modifiers():
    rec = _rec([{"kind": "key", "key": "Return", "modifiers": ["command"]}])
    script = Translator().translate(rec)
    s = script.steps[0]
    assert s.verb == "press"
    assert s.guard_kind == "none"
    assert s.action["key"] == "Return"
    assert "command+Return" in s.target_desc


def test_describer_overrides_fallback_when_screenshot_present():
    rec = _rec([{"kind": "click", "at": [1.0, 2.0]}])
    # attach a fake screenshot so describer path triggers
    rec.steps[0].screenshot_b64 = "PNG"
    script = Translator(describer=lambda b64: "the blue Submit button").translate(rec)
    assert script.steps[0].target_desc == "the blue Submit button"


def test_wait_translates_to_none_guard():
    rec = _rec([{"kind": "wait"}])
    script = Translator().translate(rec)
    assert script.steps[0].verb == "wait"
    assert script.steps[0].guard_kind == "none"


def test_script_save_load_roundtrip():
    rec = _rec(
        [
            {"kind": "click", "at": [1.0, 2.0]},
            {"kind": "type", "text": "hi"},
            {"kind": "key", "key": "Q", "modifiers": ["command"]},
        ]
    )
    script = Translator().translate(rec)
    d = tempfile.mkdtemp()
    path = os.path.join(d, "script.json")
    Translator.save(script, path)
    loaded = Translator.load(path)
    assert len(loaded.steps) == 3
    assert loaded.steps[2].verb == "press"
    os.remove(path)
    os.rmdir(d)
