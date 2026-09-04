"""Fast/Slow dual-core reasoning (PRD F2.2 / Phase 2.1).

Fast core (7B) handles routine click/scroll/type in a single VLM shot.
Slow core (27B+) is woken on: low Fast confidence, unknown dialog, or a
failed frame assertion in the step history. Slow reasons over a SOM-marked
image + full history and returns an ordered sub-step plan.

Escalation is deterministic code (Rule 5): thresholds + flags, not a second
model judging the first. Fast success never calls Slow (saves compute).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fusion_core import get_logger

from os_agent.adapters.base import Screenshot
from os_agent.config import OsaConfig

log = get_logger("os_agent.reasoning")


@dataclass
class Reason:
    action: str
    target: str = ""
    confidence: float = 0.0
    core: str = "fast"
    rationale: str = ""
    sub_steps: list[dict] = field(default_factory=list)
    escalated: bool = False

    @property
    def ok(self) -> bool:
        return self.action not in ("", "none", "halt")


@dataclass
class FastProposal:
    action: str
    target: str
    confidence: float
    unknown_dialog: bool = False


class Reasoner:
    """Fast/Slow dual-core scheduler."""

    def __init__(self, cfg: OsaConfig, mlx, som) -> None:
        self.cfg = cfg
        self.mlx = mlx
        self.som = som
        self.fast_confidence_floor = cfg.fast_confidence_floor

    async def decide(self, query: str, shot: Screenshot, history: list[dict] | None = None) -> Reason:
        history = history or []
        proposal = await self._fast_propose(query, shot)
        escalate, reason = self._should_escalate(proposal, history)
        if not escalate:
            log.info("fast accept: action=%s conf=%.2f query=%r", proposal.action, proposal.confidence, query)
            return Reason(
                action=proposal.action,
                target=proposal.target,
                confidence=proposal.confidence,
                core="fast",
                rationale="fast single-shot",
            )
        log.warning("escalate to slow: %s query=%r", reason, query)
        return await self._slow_plan(query, shot, history)

    async def _fast_propose(self, query: str, shot: Screenshot) -> FastProposal:
        prompt = (
            "You are a Fast GUI agent. Given the screenshot and the goal, pick ONE next action. "
            f"Goal: {query}. "
            'Return ONLY JSON: {"action": "click|scroll|type|key|drag|wait|none", '
            '"target": "<element label or coords>", "confidence": <0-1>, '
            '"unknown_dialog": <true if an unexpected modal/dialog blocks the goal>}. '
            "Be decisive; routine clicks need no deliberation."
        )
        try:
            data = await self.mlx.chat_json(prompt, shot.png_b64 or "", model=self.cfg.fast_model)
        except Exception as e:
            log.error("fast propose failed: %s — force escalate", e)
            return FastProposal(action="none", target="", confidence=0.0, unknown_dialog=True)
        return FastProposal(
            action=str(data.get("action", "none")).lower(),
            target=str(data.get("target", "")),
            confidence=float(data.get("confidence", 0.0)),
            unknown_dialog=bool(data.get("unknown_dialog", False)),
        )

    async def _slow_plan(self, query: str, shot: Screenshot, history: list[dict]) -> Reason:
        view = await self.som.annotate(shot)
        hist_blob = "; ".join(f'step={h.get("step")} action={h.get("action")} ok={h.get("action_ok")}' for h in history[-8:])
        prompt = (
            "You are a Slow GUI planner. The Fast core was uncertain or blocked. "
            f"Goal: {query}. Recent history: [{hist_blob}]. "
            "The image has numbered Set-of-Mark boxes over interactive elements. "
            'Return ONLY JSON: {"action": "click|type|key|scroll|drag|wait|halt", '
            '"target": "<mark number or label>", "confidence": <0-1>, '
            '"rationale": "<why>", "sub_steps": [{"action": "...", "target": "..."}, ...]}. '
            "Prefer a mark number from the image when visible."
        )
        try:
            data = await self.mlx.chat_json(prompt, view.marked_b64 or shot.png_b64 or "", model=self.cfg.slow_model)
        except Exception as e:
            log.error("slow plan failed: %s", e)
            return Reason(action="halt", core="slow", escalated=True, rationale=f"slow error: {e}")
        sub = data.get("sub_steps") or []
        if isinstance(sub, list):
            sub = [{"action": str(s.get("action", "")), "target": str(s.get("target", ""))} for s in sub if isinstance(s, dict)]
        else:
            sub = []
        return Reason(
            action=str(data.get("action", "halt")).lower(),
            target=str(data.get("target", "")),
            confidence=float(data.get("confidence", 0.0)),
            core="slow",
            rationale=str(data.get("rationale", "")),
            sub_steps=sub,
            escalated=True,
        )

    def _should_escalate(self, proposal: FastProposal, history: list[dict]) -> tuple[bool, str]:
        if proposal.unknown_dialog:
            return True, "unknown dialog"
        if proposal.confidence < self.fast_confidence_floor:
            return True, f"low confidence {proposal.confidence:.2f} < {self.fast_confidence_floor}"
        if proposal.action in ("none", "", "halt"):
            return True, "fast returned none"
        last_assert = next((h for h in reversed(history) if h.get("assert_ok") is False), None)
        if last_assert is not None:
            return True, "last assertion failed"
        return False, ""
