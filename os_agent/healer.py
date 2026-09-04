"""Multi-locator self-healing (PRD F2.2 / Phase 1.3).

When a click misses (frame assertion fails or locator stale), re-locate the
target through a degrade chain of alternate strategies before giving up:

  1. AX label exact match        (highest precision)
  2. AX role + partial label     (UI relayout kept role, tweaked text)
  3. Visual grounding            (VLM over fresh screenshot)

Each step refreshes the screenshot + AX tree, so a relayout that moved the
target is caught. SOM (1.1) is a perception aid for the Slow core, not a heal
strategy — it overlaps ax-role substring, so it is not a separate heal step.
Target: >90% self-heal success on UI relayout scenarios.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from fusion_core import get_logger

from os_agent.adapters.base import Locator, Screenshot
from os_agent.config import OsaConfig
from os_agent.perception import Perception

log = get_logger("os_agent.healer")


@dataclass
class HealResult:
    ok: bool
    locator: Locator | None
    strategy: str
    attempts: list[dict] = field(default_factory=list)
    error: str | None = None


class Healer:
    """Degrade-chain self-healing: AX-label → AX-role → visual."""

    def __init__(self, cfg: OsaConfig, perception: Perception) -> None:
        self.cfg = cfg
        self.perception = perception

    async def heal(self, query: str, last_locator: Locator | None = None) -> HealResult:
        attempts: list[dict] = []
        shot = await self.perception.capture(prefer_ax=True)

        loc = self._try_ax_label(shot, query)
        attempts.append({"strategy": "ax-label", "ok": loc is not None})
        if loc:
            log.info("heal: ax-label hit query=%r", query)
            return HealResult(ok=True, locator=loc, strategy="ax-label", attempts=attempts)

        loc = self._try_ax_role(shot, query)
        attempts.append({"strategy": "ax-role", "ok": loc is not None})
        if loc:
            log.info("heal: ax-role hit query=%r", query)
            return HealResult(ok=True, locator=loc, strategy="ax-role", attempts=attempts)

        pr = await self.perception.locate(query, shot=shot)
        attempts.append({"strategy": "visual", "ok": pr.locator.x is not None})
        if pr.locator.x is not None:
            log.info("heal: visual hit query=%r", query)
            return HealResult(ok=True, locator=pr.locator, strategy="visual", attempts=attempts)

        log.warning("heal: all strategies exhausted query=%r", query)
        return HealResult(ok=False, locator=None, strategy="exhausted", attempts=attempts, error="target not found")

    def _try_ax_label(self, shot: Screenshot, query: str) -> Locator | None:
        if not shot.node_tree:
            return None
        try:
            tree = json.loads(shot.node_tree)
        except json.JSONDecodeError:
            return None
        node = _search_ax_label(tree, query.lower())
        if node is None:
            return None
        return _node_to_locator(node, query, self.cfg.scale_factor, "ax-label")

    def _try_ax_role(self, shot: Screenshot, query: str) -> Locator | None:
        if not shot.node_tree:
            return None
        try:
            tree = json.loads(shot.node_tree)
        except json.JSONDecodeError:
            return None
        role_hint = _guess_role(query)
        node = _search_ax_role(tree, query.lower(), role_hint)
        if node is None:
            return None
        return _node_to_locator(node, query, self.cfg.scale_factor, "ax-role")


def _search_ax_label(node: dict, q: str) -> dict | None:
    label = str(node.get("label") or node.get("title") or "").lower()
    if q == label:
        return node
    for child in node.get("children") or []:
        found = _search_ax_label(child, q)
        if found:
            return found
    return None


def _search_ax_role(node: dict, q: str, role_hint: str) -> dict | None:
    role = str(node.get("role") or "").lower()
    label = str(node.get("label") or node.get("title") or "").lower()
    if role_hint and role == role_hint and q in label:
        return node
    if not role_hint and q in label:
        return node
    for child in node.get("children") or []:
        found = _search_ax_role(child, q, role_hint)
        if found:
            return found
    return None


def _guess_role(query: str) -> str:
    q = query.lower()
    if "button" in q or q in ("ok", "cancel", "submit", "confirm"):
        return "axbutton"
    if "link" in q:
        return "axlink"
    if "field" in q or "input" in q or "search" in q:
        return "axtextfield"
    if "menu" in q:
        return "axmenuitem"
    if "check" in q:
        return "axcheckbox"
    return ""


def _node_to_locator(node: dict, query: str, scale: float, strategy: str) -> Locator | None:
    frame = node.get("frame") or node.get("position") or node.get("ax_frame")
    if not frame or len(frame) < 4:
        return None
    from os_agent.config import pixels_to_points

    cx, cy = pixels_to_points(frame[0] + frame[2] / 2, frame[1] + frame[3] / 2, scale)
    return Locator(kind="ax", x=cx, y=cy, ax_role=node.get("role"), ax_label=node.get("label"), raw={"strategy": strategy, "query": query})
