"""Sensitive-region masking (PRD F3.5 / Phase 1.5).

Fail-closed: when there is no AX tree (WebGL/Canvas/Electron/no-AX), the whole
frame is blurred before it reaches the VLM — never the raw pixels. 100% local;
no frame or input leaves the host unmasked.

When an AX tree is present, sensitive nodes (secure roles + multilingual label
hints, see ax_tree.SENSITIVE_*) are blacked out and their labels are redacted
in the returned node_tree so no cleartext value reaches the model.
"""
from __future__ import annotations

import base64
import io
import json

from fusion_core import get_logger
from PIL import ImageDraw, ImageFilter

from os_agent import ax_tree
from os_agent.adapters.base import Screenshot

log = get_logger("os_agent.mask")

MIN_BLUR_RADIUS = 16


def _adaptive_blur_radius(img) -> int:
    # R2: a fixed radius=16 is resolution-independent — on a 4K Retina frame
    # it leaves large-font passwords legible, on a tiny frame it over-blurs.
    # Scale with the long edge so a 3160px frame gets ~98 and a 400px frame
    # gets the 16 floor.
    long_edge = max(img.width, img.height)
    return max(MIN_BLUR_RADIUS, long_edge // 32)


class SensitiveMasker:
    """Fail-closed sensitive masking before any VLM call."""

    def __init__(self) -> None:
        self.masked_count = 0

    def mask(self, shot: Screenshot) -> Screenshot:
        if not shot.png_b64:
            return shot
        tree = ax_tree.parse(shot.node_tree)
        sensitive = ax_tree.collect_sensitive(tree)
        if sensitive:
            masked_b64 = self._blackout(shot.png_b64, sensitive)
            redacted_tree = self._redact_tree(tree)
            log.info("mask: %d sensitive region(s) masked", len(sensitive))
            self.masked_count += len(sensitive)
            return Screenshot(
                png_b64=masked_b64,
                width=shot.width,
                height=shot.height,
                scale_factor=shot.scale_factor,
                node_tree=redacted_tree,
                meta={**shot.meta, "masked": len(sensitive)},
            )
        # No AX-tree-declared sensitive regions: if there is NO ax tree at all
        # (WebGL/Canvas/no-AX), fail closed — blur the whole frame rather than
        # ship raw pixels that may contain a password field the AX API missed.
        if not shot.node_tree:
            blurred = self._blur_all(shot.png_b64)
            log.warning("mask: no AX tree — fail-closed full-frame blur")
            return Screenshot(
                png_b64=blurred,
                width=shot.width,
                height=shot.height,
                scale_factor=shot.scale_factor,
                node_tree=None,
                meta={**shot.meta, "masked": "fail-closed-blur"},
            )
        return shot

    def _blackout(self, png_b64: str, nodes: list[ax_tree.AxNode]) -> str:
        from os_agent import image_cache

        img = image_cache.get_image(png_b64).copy()
        draw = ImageDraw.Draw(img)
        for n in nodes:
            if not n.has_frame:
                continue
            x, y, w, h = n.frame
            draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    def _blur_all(self, png_b64: str) -> str:
        from os_agent import image_cache

        img = image_cache.get_image(png_b64)
        blurred = img.filter(ImageFilter.GaussianBlur(radius=_adaptive_blur_radius(img)))
        buf = io.BytesIO()
        blurred.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    def _redact_tree(self, tree: ax_tree.AxNode | None) -> str:
        redacted = ax_tree.strip_sensitive_labels(tree)
        return json.dumps(_to_dict(redacted)) if redacted else "{}"


def _to_dict(node: ax_tree.AxNode) -> dict:
    # R5: iterative copy with a depth cap so a deeply-nested tree cannot
    # RecursionError-crash the masking path. Children are appended in order
    # by walking the tree iteratively with a stack of (node, parent_dict, depth).
    root_d: dict = {"role": node.role}
    if node.label:
        root_d["label"] = node.label
    if node.title:
        root_d["title"] = node.title
    if node.frame:
        root_d["frame"] = node.frame
    if node.identifier:
        root_d["identifier"] = node.identifier
    stack: list[tuple[ax_tree.AxNode, dict, int]] = [(node, root_d, 0)]
    max_depth = 256
    while stack:
        cur, parent_d, depth = stack.pop()
        if depth >= max_depth:
            log.warning("_to_dict capped at depth %d", max_depth)
            continue
        if cur.children:
            parent_d["children"] = []
            for c in cur.children:
                cd: dict = {"role": c.role}
                if c.label:
                    cd["label"] = c.label
                if c.title:
                    cd["title"] = c.title
                if c.frame:
                    cd["frame"] = c.frame
                if c.identifier:
                    cd["identifier"] = c.identifier
                parent_d["children"].append(cd)
                stack.append((c, cd, depth + 1))
    return root_d
