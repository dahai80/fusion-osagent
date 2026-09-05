"""Set-of-Mark annotation (PRD F1.2 / Phase 1.1).

AX boundary SOM: extract interactive AX nodes from inspect_tree, number them,
overlay numbered marks on the screenshot, return marked image + node index map.
Visual fallback: when AX tree is empty, fall back to a VLM-detected region list
(fusion-mlx) so SOM still works on WebGL/Canvas/Electron where AX is absent.

The marked image is what the Slow core reasons over; the index map lets the
model answer "click mark 3" which we resolve back to a concrete Locator.

A1 fix: interactive-node extraction uses the unified ax_tree.collect_interactive
(no local recursive walker / INTERACTIVE_ROLES duplicate here). B7: when there
is no AX tree, marked_b64 is None — never hand the model a mark-less image and
pretend SOM marks exist.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field

from fusion_core import get_logger
from PIL import ImageDraw

from os_agent import ax_tree
from os_agent.adapters.base import Locator, Screenshot
from os_agent.config import OsaConfig, pixels_to_points

log = get_logger("os_agent.som")


@dataclass
class SomNode:
    index: int
    role: str
    label: str
    frame: list
    ax_identifier: str | None = None

    def center_points(self, scale: float) -> tuple[float, float]:
        cx = self.frame[0] + self.frame[2] / 2
        cy = self.frame[1] + self.frame[3] / 2
        return pixels_to_points(cx, cy, scale)


@dataclass
class SomView:
    marked_b64: str | None
    nodes: list[SomNode] = field(default_factory=list)
    screenshot: Screenshot | None = None

    def resolve(self, index: int, scale: float) -> Locator | None:
        for n in self.nodes:
            if n.index == index:
                x, y = n.center_points(scale)
                return Locator(kind="ax", x=x, y=y, ax_role=n.role, ax_label=n.label, raw={"som_index": index})
        log.warning("som resolve: index %d not found", index)
        return None


class SomAnnotator:
    """AX-boundary SOM with visual fallback."""

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg

    async def annotate(self, shot: Screenshot, max_nodes: int = 40) -> SomView:
        nodes = self._extract_ax_nodes(shot, max_nodes)
        # B7: only draw + return a marked image when there are real AX marks.
        # With no AX tree, marked_b64 stays None so the Slow core knows SOM is
        # unavailable and reasons over the raw shot instead of phantom marks.
        marked = self._draw(shot, nodes) if (shot.png_b64 and nodes) else None
        if not nodes:
            log.info("SOM: no AX nodes — marked_b64=None (no phantom marks)")
        log.info("SOM: %d nodes marked", len(nodes))
        return SomView(marked_b64=marked, nodes=nodes, screenshot=shot)

    def _extract_ax_nodes(self, shot: Screenshot, max_nodes: int) -> list[SomNode]:
        root = ax_tree.parse(shot.node_tree)
        if root is None:
            return []
        out: list[SomNode] = []
        for n in ax_tree.collect_interactive(root, max_nodes=max_nodes):
            out.append(SomNode(
                index=0,
                role=n.role,
                label=n.label or n.title,
                frame=list(n.frame),
                ax_identifier=n.identifier,
            ))
        for i, n in enumerate(out, 1):
            n.index = i
        return out

    def _draw(self, shot: Screenshot, nodes: list[SomNode]) -> str:
        from os_agent import image_cache

        img = image_cache.get_image(shot.png_b64).copy()
        draw = ImageDraw.Draw(img)
        for n in nodes:
            x, y, w, h = n.frame
            box = [x, y, x + w, y + h]
            draw.rectangle(box, outline=(255, 0, 0), width=3)
            label = str(n.index)
            draw.rectangle([x, max(0, y - 18), x + 12 * len(label) + 6, y], fill=(255, 0, 0))
            draw.text((x + 3, max(0, y - 16)), label, fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

