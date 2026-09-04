"""Trajectory translator / generalizer (PRD F4.2 / Phase 3).

Convert a recorded `Recording` of fixed-coord Steps into a natural-language
script whose each entry carries a Semantic Guard: instead of replaying the
exact pixel, the replayer re-locates the element by AX/visual description and
only then acts — so the script generalizes across window moves, resizes, and
DPI changes.

Translation is deterministic code, not a model call (Rule 5): the action kind
maps to a verb, the recorded screenshot context (if present) is described by
its recorded kind, and the guard is derived from the step's parameters. A
VLM-describer hook is optional (lazily called when a screenshot is available
and a describer is supplied) so offline tests stay model-free.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from fusion_core import get_logger

from os_agent.recorder import Recording, Step

log = get_logger("os_agent.translator")

# kind -> human verb
VERB = {
    "click": "click",
    "double_click": "double-click",
    "right_click": "right-click",
    "type": "type",
    "key": "press",
    "scroll": "scroll",
    "drag_start": "drag from",
    "drag_end": "drop at",
    "wait": "wait",
}


@dataclass
class ScriptStep:
    """One generalized step: a verb + a semantic target + a guard."""

    seq: int
    verb: str
    target_desc: str  # natural-language element description (for AX/visual re-locate)
    guard_kind: str  # ax | visual | point | none
    action: dict = field(default_factory=dict)  # raw params the replayer needs (key, text, modifiers, at, drag_to)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "verb": self.verb,
            "target_desc": self.target_desc,
            "guard_kind": self.guard_kind,
            "action": self.action,
            "note": self.note,
        }


@dataclass
class Script:
    steps: list[ScriptStep] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"steps": [s.to_dict() for s in self.steps], "meta": self.meta},
            indent=2,
            ensure_ascii=False,
        )


class Translator:
    """Recording -> Script with Semantic Guards."""

    def __init__(self, describer: Callable[[str | None], str] | None = None) -> None:
        # describer(png_b64) -> short element description; optional, model-backed
        self.describer = describer

    def translate(self, rec: Recording) -> Script:
        script = Script(meta=dict(rec.meta))
        for step in rec.steps:
            sstep = self._translate_step(step)
            script.steps.append(sstep)
            log.info("translated step %d: %s '%s' guard=%s", step.seq, sstep.verb, sstep.target_desc, sstep.guard_kind)
        log.info("translation done: %d steps", len(script.steps))
        return script

    def _translate_step(self, step: Step) -> ScriptStep:
        verb = VERB.get(step.kind, step.kind)
        target_desc, guard_kind, action = self._derive(step)
        return ScriptStep(
            seq=step.seq,
            verb=verb,
            target_desc=target_desc,
            guard_kind=guard_kind,
            action=action,
        )

    def _derive(self, step: Step) -> tuple[str, str, dict]:
        # default action payload
        action: dict = {}
        if step.kind in ("click", "double_click", "right_click"):
            action = {"at": step.at, "button": step.button or "left"}
            desc = self._describe(step, fallback=f"control at point {step.at}")
            return desc, "visual", action
        if step.kind == "type":
            action = {"text": step.text}
            # typing targets the focused field; guard by visual locate of an input near the last point
            desc = self._describe(step, fallback="focused text field")
            return desc, "visual", action
        if step.kind == "key":
            mods = "+".join(step.modifiers + [step.key]) if step.modifiers else step.key
            action = {"key": step.key, "modifiers": step.modifiers}
            return f"keyboard shortcut {mods}", "none", action
        if step.kind == "scroll":
            action = {"at": step.at}
            return self._describe(step, fallback=f"scroll area at {step.at}"), "visual", action
        if step.kind in ("drag_start", "drag_end"):
            action = {"at": step.at, "drag_to": step.drag_to}
            return self._describe(step, fallback=f"drag handle near {step.at}"), "visual", action
        if step.kind == "wait":
            return "wait for UI to settle", "none", {}
        return step.kind, "none", {}

    def _describe(self, step: Step, fallback: str) -> str:
        if self.describer and step.screenshot_b64:
            try:
                desc = self.describer(step.screenshot_b64)
                if desc:
                    return desc.strip()
            except Exception as e:
                log.warning("describer failed: %s — using fallback", e)
        return fallback

    @staticmethod
    def save(script: Script, path: str) -> str:
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(script.to_json())
        log.info("script saved: %s", path)
        return path

    @staticmethod
    def load(path: str) -> Script:
        data = json.loads(open(path).read())
        steps = [
            ScriptStep(
                seq=s["seq"],
                verb=s["verb"],
                target_desc=s["target_desc"],
                guard_kind=s.get("guard_kind", "none"),
                action=s.get("action", {}),
                note=s.get("note", ""),
            )
            for s in data.get("steps", [])
        ]
        return Script(steps=steps, meta=data.get("meta", {}))
