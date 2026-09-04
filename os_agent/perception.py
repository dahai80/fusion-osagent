"""Dual-track perception: AX-first, visual-fallback (PRD F1.1).

AX track: fusion-executor inspect_tree → structured AX node tree (high precision).
Visual track: fusion-mlx VLM over screenshot (fallback when AX unavailable:
  WebGL/Canvas/Electron lag/custom-drawn controls), or fusion-browser for Web.

Arbitration: when both tracks return and disagree beyond a tolerance, flag for
Slow-core arbitration (reasoning.py Phase 2). Phase 0 records the divergence,
Phase 2 acts on it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from fusion_core import get_logger

from os_agent.adapters.base import Locator, Screenshot
from os_agent.adapters.browser import BrowserAdapter, StubBrowserAdapter
from os_agent.adapters.executor import ExecutorAdapter, StubExecutorAdapter
from os_agent.adapters.mlx import MlxAdapter, StubMlxAdapter
from os_agent.config import OsaConfig

log = get_logger("os_agent.perception")

GROUNDING_PROMPT = (
    "You are a GUI grounding model. Given this screenshot, find the element matching: "
    "{query}. Return ONLY JSON: {{\"x\": <float 0.0-1.0>, \"y\": <float 0.0-1.0>, "
    "\"confidence\": <float 0.0-1.0>, \"label\": \"<text>\"}}. "
    "x and y MUST be normalized fractions of image width/height (0.0=top-left, 1.0=bottom-right). "
    "NEVER return pixel counts. Example: center of a 800x600 image is x=0.5, y=0.5."
)

VISUAL_FALLBACK_HINTS = ("webgl", "canvas", "electron", "custom-draw", "no-ax", "ax-empty")


@dataclass
class PerceptionResult:
    locator: Locator
    track: str
    screenshot: Screenshot
    confidence: float = 1.0
    ax_available: bool = True
    divergent: bool = False
    meta: dict = field(default_factory=dict)


class Perception:
    """Dual-track scheduler: AX (executor) → visual (mlx/browser)."""

    def __init__(
        self,
        cfg: OsaConfig,
        executor: ExecutorAdapter | StubExecutorAdapter,
        mlx: MlxAdapter | StubMlxAdapter,
        browser: BrowserAdapter | StubBrowserAdapter | None = None,
    ) -> None:
        self.cfg = cfg
        self.executor = executor
        self.mlx = mlx
        self.browser = browser

    async def capture(self, prefer_ax: bool = True) -> Screenshot:
        if prefer_ax:
            try:
                shot = await self.executor.inspect_tree()
                if shot.node_tree:
                    return shot
                log.info("AX tree empty, fallback to plain screenshot")
            except Exception as e:
                log.warning("AX inspect failed: %s — fallback to screenshot", e)
        return await self.executor.screenshot()

    async def locate(self, query: str, shot: Screenshot | None = None) -> PerceptionResult:
        shot = shot or await self.capture(prefer_ax=True)
        if shot.has_ax:
            loc = self._find_in_ax(shot.node_tree, query)
            if loc is not None:
                log.info("AX locate hit: query=%r -> %s", query, loc)
                return PerceptionResult(locator=loc, track="ax", screenshot=shot, confidence=0.95, ax_available=True)
            log.info("AX tree present but no match for %r — visual fallback", query)
        loc = await self._locate_visual(query, shot)
        return PerceptionResult(
            locator=loc,
            track="visual",
            screenshot=shot,
            confidence=loc.raw.get("confidence", 0.6),
            ax_available=shot.has_ax,
        )

    def _find_in_ax(self, node_tree: str | None, query: str) -> Locator | None:
        if not node_tree:
            return None
        try:
            tree = json.loads(node_tree)
        except json.JSONDecodeError as e:
            log.warning("AX tree parse failed: %s", e)
            return None
        q = query.lower()
        node = _search_ax(tree, q)
        if node is None:
            return None
        frame = node.get("frame") or node.get("position") or node.get("ax_frame")
        if not frame or len(frame) < 4:
            return None
        x, y, w, h = frame[0], frame[1], frame[2], frame[3]
        from os_agent.config import pixels_to_points

        cx, cy = pixels_to_points(x + w / 2, y + h / 2, self.cfg.scale_factor)
        return Locator(
            kind="ax",
            x=cx,
            y=cy,
            ax_role=node.get("role"),
            ax_label=node.get("label") or node.get("title"),
            raw={"frame": frame, "matched": query},
        )

    async def _locate_visual(self, query: str, shot: Screenshot) -> Locator:
        if not shot.png_b64:
            log.error("no screenshot for visual locate")
            return Locator(kind="visual", x=0.0, y=0.0, visual_query=query, raw={"confidence": 0.0})
        prompt = GROUNDING_PROMPT.format(query=query)
        try:
            data = await self.mlx.chat_json(prompt, shot.png_b64)
        except Exception as e:
            log.error("visual locate mlx failed: %s", e)
            return Locator(kind="visual", x=0.0, y=0.0, visual_query=query, raw={"confidence": 0.0, "error": str(e)})
        nx = float(data.get("x", 0.0))
        ny = float(data.get("y", 0.0))
        conf = float(data.get("confidence", 0.0))
        # VLM output is non-deterministic: may return normalized (0-1) OR raw pixels.
        # Normalize to points in the API's logical-point space.
        if shot.width and shot.height:
            if 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0:
                x = nx * shot.width / self.cfg.scale_factor
                y = ny * shot.height / self.cfg.scale_factor
            else:
                # raw physical pixels -> logical points
                x = nx / self.cfg.scale_factor
                y = ny / self.cfg.scale_factor
                log.warning("visual locate: VLM returned raw pixels (not normalized); converting")
        else:
            x, y = nx, ny
        log.info("visual locate: query=%r raw=(%.3f,%.3f) point=(%.1f,%.1f) conf=%.2f", query, nx, ny, x, y, conf)
        return Locator(kind="visual", x=x, y=y, visual_query=query, raw={"confidence": conf, "label": data.get("label")})


def _search_ax(node: dict, query: str) -> dict | None:
    label = (str(node.get("label") or node.get("title") or node.get("text") or "")).lower()
    role = str(node.get("role") or "").lower()
    if query in label or query in role:
        return node
    for child in node.get("children") or []:
        found = _search_ax(child, query)
        if found:
            return found
    return None
