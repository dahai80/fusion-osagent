"""Dual-track perception: AX-first, visual-fallback (PRD F1.1).

AX track: fusion-executor inspect_tree → structured AX node tree (high precision).
Visual track: fusion-mlx VLM over screenshot (fallback when AX unavailable:
  WebGL/Canvas/Electron lag/custom-drawn controls), or fusion-browser for Web.

Coordinate space: API surface is logical points. AX frames are physical pixels
(converted via pixels_to_points). VLM output is normalized 0-1 fractions of the
screenshot's physical dimensions; raw-pixel output is rejected as ambiguous
(D4) so the caller never clicks a wrong unit. Failed visual inference returns
x=None (D5 fail-loud) — never a (0,0) default that would silently click the
top-left corner.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fusion_core import get_logger

from os_agent import ax_tree
from os_agent.adapters.base import Locator, Screenshot
from os_agent.adapters.browser import BrowserAdapter, StubBrowserAdapter
from os_agent.adapters.executor import ExecutorAdapter, StubExecutorAdapter
from os_agent.adapters.mlx import MlxAdapter, StubMlxAdapter
from os_agent.config import OsaConfig, pixels_to_points
from os_agent.mask import SensitiveMasker

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
        vlm_cache=None,
        masker: SensitiveMasker | None = None,
    ) -> None:
        self.cfg = cfg
        self.executor = executor
        self.mlx = mlx
        self.browser = browser
        # E4: accept a shared masker so reasoner + perception use one instance
        # (unified masked_count + a single LRU). Falls back to its own only when
        # constructed standalone (tests).
        self.masker = masker if masker is not None else SensitiveMasker()
        # R6: visual locate is the biggest inference spender (replayer calls it
        # every step) yet bypassed the reasoner's vlm_cache. Reuse the same
        # cache so a repeated identical (prompt, masked image) skips re-infer.
        self.vlm_cache = vlm_cache

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
            loc, conf = self._find_in_ax(shot, query)
            if loc is not None:
                log.info("AX locate hit: query=%r -> %s conf=%.2f", query, loc, conf)
                return PerceptionResult(locator=loc, track="ax", screenshot=shot, confidence=conf, ax_available=True)
            log.info("AX tree present but no match for %r — visual fallback", query)
        loc = await self._locate_visual(query, shot)
        return PerceptionResult(
            locator=loc,
            track="visual",
            screenshot=shot,
            confidence=loc.raw.get("confidence", 0.6),
            ax_available=shot.has_ax,
        )

    def _find_in_ax(self, shot: Screenshot, query: str) -> tuple[Locator | None, float]:
        """AX locate with graded confidence (B1) and exact-first matching (B2).

        Short/generic queries used to substring-match almost any node ("a" hit
        the first label containing "a"). Now: require a minimum query length,
        try exact -> prefix -> substring in that order, and report a confidence
        that reflects match precision (exact > prefix > substring) so a fuzzy
        hit no longer carries a hardcoded 0.95 that skips visual cross-check.
        Returns (locator, confidence); (None, 0.0) when nothing usable matches.
        """
        root = ax_tree.parse(shot.node_tree)
        if root is None:
            return None, 0.0
        q = query.strip()
        # B2: refuse degenerate short queries that substring-match everything.
        if len(q) < 2:
            log.info("AX query too short (%r) — skip AX match", q)
            return None, 0.0
        node = ax_tree.find_by_label(root, q, mode="exact")
        conf = 0.95 if node else 0.0
        if node is None:
            node = ax_tree.find_by_label(root, q, mode="prefix")
            conf = 0.85 if node else 0.0
        if node is None:
            node = ax_tree.find_by_label(root, q, mode="substring")
            conf = 0.7 if node else 0.0
        if node is None:
            role = ax_tree.guess_role(q)
            if role:
                node = ax_tree.find_by_role(root, q, role)
                conf = 0.75 if node else 0.0
        if node is None or not node.has_frame:
            return None, 0.0
        # A5: prefer the per-screenshot scale (multi-display safe) over the
        # global cfg default, so a frame from a 1x external display is not
        # converted with the 2x Retina assumption.
        scale = shot.scale_factor or self.cfg.scale_factor
        cx, cy = pixels_to_points(*node.center_px(), scale)
        return Locator(
            kind="ax",
            x=cx,
            y=cy,
            ax_role=node.role,
            ax_label=node.label or node.title,
            raw={"frame": list(node.frame), "matched": query, "ax_confidence": conf},
        ), conf

    async def _locate_visual(self, query: str, shot: Screenshot) -> Locator:
        if not shot.png_b64:
            log.error("no screenshot for visual locate")
            return Locator(kind="visual", x=None, y=None, visual_query=query, raw={"confidence": 0.0, "error": "no screenshot"})
        shot = self.masker.mask(shot)  # F3.5: never feed raw sensitive pixels to VLM
        prompt = GROUNDING_PROMPT.format(query=query)
        # R6: short-circuit identical visual-locate inferences via the shared
        # vlm_cache (same scheme as the reasoner) so replayer guard re-locates
        # on an unchanged screen skip the VLM round-trip.
        model = getattr(self.mlx, "model", "")
        data = None
        if self.vlm_cache is not None:
            cached, hit = self.vlm_cache.get(model, prompt, shot.png_b64 or "")
            if hit:
                data = cached
        if data is None:
            try:
                data = await self.mlx.chat_json(prompt, shot.png_b64)
            except Exception as e:
                log.exception("visual locate mlx failed")
                return Locator(kind="visual", x=None, y=None, visual_query=query, raw={"confidence": 0.0, "error": str(e)})
            if self.vlm_cache is not None:
                self.vlm_cache.put(model, prompt, shot.png_b64 or "", data)
        if data is None:
            log.warning("visual locate: mlx returned no JSON for %r", query)
            return Locator(kind="visual", x=None, y=None, visual_query=query, raw={"confidence": 0.0, "error": "non-JSON"})
        nx = data.get("x")
        ny = data.get("y")
        conf = float(data.get("confidence", 0.0))
        if nx is None or ny is None:
            log.warning("visual locate: missing x/y for %r", query)
            return Locator(kind="visual", x=None, y=None, visual_query=query, raw={"confidence": conf, "error": "missing x/y"})
        try:
            nx = float(nx)
            ny = float(ny)
        except (TypeError, ValueError):
            log.warning("visual locate: non-numeric x/y (%r, %r)", nx, ny)
            return Locator(kind="visual", x=None, y=None, visual_query=query, raw={"confidence": conf, "error": "non-numeric x/y"})
        # D4: only accept normalized fractions (0..1). Raw pixel output is
        # ambiguous (which unit? physical vs logical?) — reject loudly rather
        # than guess a conversion. Require both dimensions to validate.
        if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
            log.warning("visual locate: VLM returned non-normalized coords (%.3f,%.3f) for %r — rejecting", nx, ny, query)
            return Locator(kind="visual", x=None, y=None, visual_query=query, raw={"confidence": conf, "error": "non-normalized coords"})
        if not (shot.width and shot.height):
            log.warning("visual locate: screenshot has no dimensions; cannot map normalized coords")
            return Locator(kind="visual", x=None, y=None, visual_query=query, raw={"confidence": conf, "error": "no screenshot dimensions"})
        scale = shot.scale_factor or self.cfg.scale_factor
        x = nx * shot.width / scale
        y = ny * shot.height / scale
        log.info("visual locate: query=%r norm=(%.3f,%.3f) point=(%.1f,%.1f) conf=%.2f", query, nx, ny, x, y, conf)
        return Locator(kind="visual", x=x, y=y, visual_query=query, raw={"confidence": conf, "label": data.get("label")})
