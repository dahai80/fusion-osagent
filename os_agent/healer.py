"""Multi-locator self-healing (PRD F2.2 / Phase 1.3).

When a click misses (frame assertion fails or locator stale), re-locate the
target through a degrade chain of alternate strategies before giving up:

  1. AX label exact match        (highest precision)
  2. AX role + partial label     (UI relayout kept role, tweaked text)
  3. Visual grounding            (VLM over fresh screenshot)

Each step refreshes the screenshot + AX tree, so a relayout that moved the
target is caught. AX queries go through the unified ax_tree module (A1 fix);
no duplicate recursive walkers here. SOM (1.1) is a perception aid for the
Slow core, not a heal strategy — it overlaps ax-role substring, so it is not a
separate heal step. Target: >90% self-heal success on UI relayout scenarios.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fusion_core import get_logger

from os_agent import ax_tree
from os_agent.adapters.base import Locator, Screenshot
from os_agent.config import OsaConfig, pixels_to_points
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
        root = ax_tree.parse(shot.node_tree)
        if root is None:
            return None
        node = ax_tree.find_by_label(root, query, mode="exact")
        return _node_to_locator(node, query, self._scale(shot), "ax-label") if node else None

    def _try_ax_role(self, shot: Screenshot, query: str) -> Locator | None:
        root = ax_tree.parse(shot.node_tree)
        if root is None:
            return None
        role_hint = ax_tree.guess_role(query)
        node = ax_tree.find_by_role(root, query, role_hint)
        return _node_to_locator(node, query, self._scale(shot), "ax-role") if node else None

    def _scale(self, shot: Screenshot) -> float:
        # A5: prefer per-screenshot scale over the global cfg default.
        return shot.scale_factor or self.cfg.scale_factor


def _node_to_locator(node: ax_tree.AxNode, query: str, scale: float, strategy: str) -> Locator | None:
    if not node.has_frame:
        return None
    cx, cy = pixels_to_points(*node.center_px(), scale)
    return Locator(kind="ax", x=cx, y=cy, ax_role=node.role, ax_label=node.label, raw={"strategy": strategy, "query": query})
