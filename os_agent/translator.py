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
        # describer(png_b64) -> short element description; optional, model-backed.
        # E6: a model-backed describer is a blocking network call. translate() is
        # synchronous, so a blocking describer holds the caller's thread for the
        # whole recording (one VLM call per step) with no way to cancel. The
        # async path (translate_async) offloads the sync describer to a worker
        # thread so the event loop is never blocked and asyncio.wait_for can
        # interrupt a stuck describe. Offline tests keep using translate() with
        # no describer (pure deterministic code, never blocks).
        self.describer = describer

    def translate(self, rec: Recording) -> Script:
        script = Script(meta=dict(rec.meta))
        for step in rec.steps:
            sstep = self._translate_step(step)
            script.steps.append(sstep)
            log.info("translated step %d: %s '%s' guard=%s", step.seq, sstep.verb, sstep.target_desc, sstep.guard_kind)
        log.info("translation done: %d steps", len(script.steps))
        return script

    async def translate_async(self, rec: Recording) -> Script:
        """E6: async translation that offloads the (blocking) describer per step.

        Each step's describe call runs in a worker thread, so a stuck VLM
        describe cannot pin the event loop or hang the whole recording. Steps
        are translated in order to preserve seq numbering; only the model call
        is concurrency-safe.
        """
        script = Script(meta=dict(rec.meta))
        for step in rec.steps:
            sstep = await self._translate_step_async(step)
            script.steps.append(sstep)
            log.info("translated step %d: %s '%s' guard=%s", step.seq, sstep.verb, sstep.target_desc, sstep.guard_kind)
        log.info("translation done: %d steps", len(script.steps))
        return script

    async def _translate_step_async(self, step: Step) -> ScriptStep:
        verb = VERB.get(step.kind, step.kind)
        target_desc, guard_kind, action = await self._derive_async(step)
        return ScriptStep(
            seq=step.seq,
            verb=verb,
            target_desc=target_desc,
            guard_kind=guard_kind,
            action=action,
        )

    async def _derive_async(self, step: Step) -> tuple[str, str, dict]:
        action: dict = {}
        if step.kind in ("click", "double_click", "right_click"):
            action = {"at": step.at, "button": step.button or "left"}
            desc, described = await self._describe_async(step)
            guard = "visual" if described else "point"
            return desc or f"control at point {step.at}", guard, action
        if step.kind == "type":
            action = {"text": step.text}
            desc, described = await self._describe_async(step)
            guard = "visual" if described else "point"
            return desc or "focused text field", guard, action
        if step.kind == "key":
            mods = "+".join(step.modifiers + [step.key]) if step.modifiers else step.key
            action = {"key": step.key, "modifiers": step.modifiers}
            return f"keyboard shortcut {mods}", "none", action
        if step.kind == "scroll":
            action = {"at": step.at}
            desc, described = await self._describe_async(step)
            guard = "visual" if described else "point"
            return desc or f"scroll area at {step.at}", guard, action
        if step.kind in ("drag_start", "drag_end"):
            action = {"at": step.at, "drag_to": step.drag_to}
            desc, described = await self._describe_async(step)
            guard = "visual" if described else "point"
            return desc or f"drag handle near {step.at}", guard, action
        if step.kind == "wait":
            return "wait for UI to settle", "none", {}
        return step.kind, "none", {}

    async def _describe_async(self, step: Step) -> tuple[str, bool]:
        """E6: offload the blocking describer to a worker thread."""
        import asyncio

        if not self.describer or not step.screenshot_b64:
            return "", False
        try:
            desc = await asyncio.to_thread(self.describer, step.screenshot_b64)
            if desc:
                return desc.strip(), True
        except Exception as e:
            log.warning("describer failed: %s — degrading to point guard", e)
        return "", False

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
        # D11: guard_kind is "visual" ONLY when a real element description
        # exists (describer + screenshot). Without one, the Semantic Guard has
        # nothing to re-locate by, so degrade to "point" (replay the recorded
        # coordinate) — never claim a visual guard that cannot be satisfied.
        action: dict = {}
        if step.kind in ("click", "double_click", "right_click"):
            action = {"at": step.at, "button": step.button or "left"}
            desc, described = self._describe(step)
            guard = "visual" if described else "point"
            return desc or f"control at point {step.at}", guard, action
        if step.kind == "type":
            action = {"text": step.text}
            desc, described = self._describe(step)
            guard = "visual" if described else "point"
            return desc or "focused text field", guard, action
        if step.kind == "key":
            mods = "+".join(step.modifiers + [step.key]) if step.modifiers else step.key
            action = {"key": step.key, "modifiers": step.modifiers}
            return f"keyboard shortcut {mods}", "none", action
        if step.kind == "scroll":
            action = {"at": step.at}
            desc, described = self._describe(step)
            guard = "visual" if described else "point"
            return desc or f"scroll area at {step.at}", guard, action
        if step.kind in ("drag_start", "drag_end"):
            action = {"at": step.at, "drag_to": step.drag_to}
            desc, described = self._describe(step)
            guard = "visual" if described else "point"
            return desc or f"drag handle near {step.at}", guard, action
        if step.kind == "wait":
            return "wait for UI to settle", "none", {}
        return step.kind, "none", {}

    def _describe(self, step: Step) -> tuple[str, bool]:
        """Return (description, was_described). Without a describer/screenshot,
        returns ("", False) so the caller degrades guard_kind to "point"."""
        if self.describer and step.screenshot_b64:
            try:
                desc = self.describer(step.screenshot_b64)
                if desc:
                    return desc.strip(), True
            except Exception as e:
                log.warning("describer failed: %s — degrading to point guard", e)
        return "", False

    @staticmethod
    def save(script: Script, path: str) -> str:
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(script.to_json())
        log.info("script saved: %s", path)
        return path

    @staticmethod
    def load(path: str) -> Script:
        with open(path) as fh:
            data = json.loads(fh.read())
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
