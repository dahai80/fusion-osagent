from __future__ import annotations

import json
import os
import tempfile

from os_agent.recorder import (
    CGEventTapSource,
    ManualEventSource,
    Recorder,
    Recording,
    Step,
)


def test_manual_source_records_steps_with_seq_and_ts():
    src = ManualEventSource(
        [
            {"kind": "click", "at": [100.0, 200.0], "button": "left"},
            {"kind": "type", "text": "hello"},
            {"kind": "key", "key": "Return", "modifiers": ["command"]},
        ]
    )
    clock = iter([1.0, 1.5, 2.0])
    rec = Recorder(src, capture=lambda: None, clock=lambda: next(clock)).record()
    assert len(rec.steps) == 3
    assert rec.steps[0].seq == 1
    assert rec.steps[0].kind == "click"
    assert rec.steps[0].at == [100.0, 200.0]
    assert rec.steps[0].ts == 1.0
    assert rec.steps[1].text == "hello"
    assert rec.steps[2].key == "Return"
    assert rec.steps[2].modifiers == ["command"]
    assert rec.steps[2].ts == 2.0


def test_recorder_attaches_screenshot_from_capture():
    src = ManualEventSource([{"kind": "click", "at": [1.0, 2.0]}])
    rec = Recorder(src, capture=lambda: "PNGB64", clock=lambda: 0.0).record()
    assert rec.steps[0].screenshot_b64 == "PNGB64"


def test_recording_save_and_load_roundtrip():
    src = ManualEventSource(
        [
            {"kind": "click", "at": [10.0, 20.0], "button": "left"},
            {"kind": "key", "key": "K", "modifiers": ["shift"]},
        ]
    )
    rec = Recorder(src, capture=lambda: None, clock=lambda: 1.0).record()
    d = tempfile.mkdtemp()
    path = os.path.join(d, "rec.json")
    Recorder.save(rec, path)
    loaded = Recorder.load(path)
    assert len(loaded.steps) == 2
    assert loaded.steps[0].at == [10.0, 20.0]
    assert loaded.steps[1].modifiers == ["shift"]
    os.remove(path)
    os.rmdir(d)


def test_recording_to_json_structure():
    rec = Recording(steps=[Step(seq=1, kind="click", at=[5.0, 6.0])], meta={"app": "X"})
    data = json.loads(rec.to_json())
    assert data["meta"]["app"] == "X"
    assert data["steps"][0]["kind"] == "click"
    assert data["steps"][0]["has_screenshot"] is False


def test_cgeventtap_source_constructs_off_darwin_without_quartz():
    # On Darwin with Quartz installed, events() starts a blocking CFRunLoop —
    # skip there. Off-Darwin (no Quartz) it yields nothing safely.
    try:
        import Quartz  # type: ignore  # noqa: F401
    except ImportError:
        src = CGEventTapSource()
        assert list(src.events()) == []
    else:
        # just assert constructible; never call events() (would block)
        assert CGEventTapSource() is not None
