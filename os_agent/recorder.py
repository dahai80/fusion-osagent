"""GUI trajectory recorder (PRD F4.1 / Phase 3).

Capture real mouse/keyboard events + the screenshot context at each event into
structured Steps. Steps feed the translator (F4.2) → natural-language script,
and the replayer (F4.3) → step-by-step replay with frame assertion.

Two event sources:
  - ManualEventSource: caller pushes events (tests, scripted capture).
  - CGEventTapSource: lazy Quartz CGEventTap (real macOS events). Quartz import
    is lazy so the module stays importable off-Darwin / in CI without pyobjc.

Recorder itself is deterministic: it assigns seq, stamps ts from a provided
clock (pluggable so tests are reproducible), and snapshots the screen through a
capture callable. No Math.random / wall-clock at module level (Rule 5).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from fusion_core import get_logger

log = get_logger("os_agent.recorder")


@dataclass
class Step:
    """One recorded primitive event with its screen context."""

    seq: int
    kind: str  # click | double_click | right_click | type | key | scroll | drag_start | drag_end | wait
    at: list[float] | None = None  # logical point [x, y]
    button: str = ""
    key: str = ""
    modifiers: list[str] = field(default_factory=list)
    text: str = ""
    drag_to: list[float] | None = None
    screenshot_b64: str | None = None
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "at": self.at,
            "button": self.button,
            "key": self.key,
            "modifiers": self.modifiers,
            "text": self.text,
            "drag_to": self.drag_to,
            "has_screenshot": self.screenshot_b64 is not None,
            "ts": self.ts,
        }


@dataclass
class Recording:
    steps: list[Step] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"steps": [s.to_dict() for s in self.steps], "meta": self.meta},
            indent=2,
            ensure_ascii=False,
        )


class EventSource:
    """Pluggable event stream. Yields raw event dicts: {kind, at, button, key, modifiers, text, drag_to}."""

    def events(self) -> Iterator[dict]:
        raise NotImplementedError

    def stop(self) -> None:
        pass


class ManualEventSource(EventSource):
    """Caller-fed event queue — for tests and scripted capture."""

    def __init__(self, events: list[dict] | None = None) -> None:
        self._events = list(events or [])

    def push(self, ev: dict) -> None:
        self._events.append(ev)

    def events(self) -> Iterator[dict]:
        pending = list(self._events)
        self._events.clear()
        yield from pending

    def stop(self) -> None:
        pass


class CGEventTapSource(EventSource):
    """Real macOS CGEventTap source. Quartz imported lazily.

    Not exercised by tests (needs Accessibility TCC); kept for real capture.
    """

    def __init__(self) -> None:
        self._tap = None
        self._queue: list[dict] = []
        self._stopped = False

    def events(self) -> Iterator[dict]:
        try:
            import Quartz  # type: ignore
            from CoreFoundation import CFRelease  # type: ignore
        except ImportError as e:
            log.error("Quartz unavailable — cannot tap events: %s", e)
            return

        scale = 2.0  # logical-point space; refined when E1 exposes scale_factor

        def _callback(_proxy, _type, event, _refcon):
            if self._stopped:
                return event
            ev = self._map_event(event, scale)
            if ev:
                self._queue.append(ev)
            return event

        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            0,
            _callback,
            None,
        )
        if tap is None:
            log.error("CGEventTapCreate returned None — Accessibility permission missing")
            return
        self._tap = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopDefaultMode)
        log.info("CGEventTap active — run loop starting")
        # Run loop blocks; callers iterate in a thread. Simpler: drain queue in
        # a non-blocking loop driven by the recorder. For now we expose stop().
        try:
            Quartz.CFRunLoopRun()
        finally:
            Quartz.CFRunLoopStop(Quartz.CFRunLoopGetCurrent())
            CFRelease(tap)
            self._tap = None

    def _map_event(self, event, scale: float) -> dict | None:
        try:
            import Quartz  # type: ignore
        except ImportError:
            return None
        etype = Quartz.CGEventGetType(event)
        loc = Quartz.CGEventGetLocation(event)
        x, y = loc.x / scale, loc.y / scale
        if etype == Quartz.kCGEventLeftMouseDown:
            return {"kind": "click", "at": [x, y], "button": "left"}
        if etype == Quartz.kCGEventRightMouseDown:
            return {"kind": "right_click", "at": [x, y], "button": "right"}
        if etype == Quartz.kCGEventKeyDown:
            flags = Quartz.CGEventGetFlags(event)
            mods = self._flags_to_mods(flags)
            text = Quartz.CGEventKeyboardGetUnicodeString(event, 100, None)
            return {"kind": "key", "key": text or "", "modifiers": mods, "text": text or ""}
        return None

    @staticmethod
    def _flags_to_mods(flags: int) -> list[str]:
        mods = []
        # Quartz kCGEventFlagMask* bit masks
        if flags & 0x020000:  # shift
            mods.append("shift")
        if flags & 0x040000:  # control
            mods.append("control")
        if flags & 0x080000:  # command
            mods.append("command")
        if flags & 0x200000:  # option
            mods.append("option")
        return mods

    def stop(self) -> None:
        self._stopped = True
        try:
            import Quartz  # type: ignore

            Quartz.CFRunLoopStop(Quartz.CFRunLoopGetCurrent())
        except ImportError:
            pass


class Recorder:
    """Turn an event stream into a Recording of Steps, each with a screenshot."""

    def __init__(
        self,
        source: EventSource,
        capture: Callable[[], str | None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.source = source
        self.capture = capture  # returns png b64 of current frame, or None
        self.clock = clock
        self._seq = 0

    def record(self) -> Recording:
        rec = Recording()
        for ev in self.source.events():
            self._seq += 1
            shot = self.capture() if self.capture else None
            ts = self.clock() if self.clock else 0.0
            step = Step(
                seq=self._seq,
                kind=str(ev.get("kind", "wait")),
                at=list(ev["at"]) if ev.get("at") is not None else None,
                button=str(ev.get("button", "")),
                key=str(ev.get("key", "")),
                modifiers=list(ev.get("modifiers", [])),
                text=str(ev.get("text", "")),
                drag_to=list(ev["drag_to"]) if ev.get("drag_to") is not None else None,
                screenshot_b64=shot,
                ts=ts,
            )
            rec.steps.append(step)
            log.info("recorded step %d: %s at=%s", step.seq, step.kind, step.at)
        self.source.stop()
        log.info("recording done: %d steps", len(rec.steps))
        return rec

    @staticmethod
    def save(rec: Recording, path: str) -> str:
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(rec.to_json())
        log.info("recording saved: %s", path)
        return path

    @staticmethod
    def load(path: str) -> Recording:
        data = json.loads(open(path).read())
        steps = [
            Step(
                seq=s["seq"],
                kind=s["kind"],
                at=s.get("at"),
                button=s.get("button", ""),
                key=s.get("key", ""),
                modifiers=s.get("modifiers", []),
                text=s.get("text", ""),
                drag_to=s.get("drag_to"),
                screenshot_b64=None,  # screenshots not persisted in the script
                ts=s.get("ts", 0.0),
            )
            for s in data.get("steps", [])
        ]
        return Recording(steps=steps, meta=data.get("meta", {}))
