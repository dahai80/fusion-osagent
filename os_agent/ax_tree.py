"""Unified AX-tree parser + query (PRD A1 fix).

Single source of truth for AX node-tree walking. Replaces the five duplicate
recursive walkers in perception / healer / som / mask. All coordinate frames
are physical pixels (what the AX API returns); callers convert to logical
points via pixels_to_points.

Query modes:
  - exact:      label == query
  - prefix:     label.startswith(query)
  - substring:  query in label   (case-insensitive)
  - role:       role == role_hint AND substring(label, query)
  - interactive:role in INTERACTIVE_ROLES (with a usable frame)
  - sensitive:  role in SENSITIVE_ROLES OR label hints at sensitive content

Iterative walk (no recursion depth risk) with a hard node cap so a 500-node
window tree cannot blow the stack or cost too much.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from fusion_core import get_logger

log = get_logger("os_agent.ax_tree")

MAX_NODES = 2000

INTERACTIVE_ROLES = {
    "axbutton", "axcheckbox", "axradio", "axmenuitem", "axmenu", "axcombobox",
    "axslider", "axtextfield", "axtextarea", "axsecuretextfield", "axlink",
    "axpopout", "axdisclosure", "axtoolbar", "axtab", "axgrowarea",
}

SENSITIVE_ROLES = {
    "axsecuretextfield", "axpasswordfield", "axsecuretextarea",
}

# Multilingual sensitive label hints (case-insensitive substring/regex).
SENSITIVE_LABEL_PATTERNS = [
    r"pass(word|wd)?", r"passwd", r"secret", r"token", r"api[- ]?key",
    r"credential", r"\bpin\b", r"otp", r"cvv", r"card[- ]?number",
    # CJK / ja / ko
    "密码", "密碼", "パスワード", "비밀번호", "비번",
    # "your secret code" style without the english keywords above
    "secret code", "access code",
]
_SENSITIVE_RE = re.compile("|".join(SENSITIVE_LABEL_PATTERNS), re.IGNORECASE)


@dataclass
class AxNode:
    role: str
    label: str
    title: str
    frame: list[float] | None  # [x, y, w, h] physical px
    identifier: str | None = None
    children: list[AxNode] = field(default_factory=list)

    @property
    def is_sensitive(self) -> bool:
        if self.role in SENSITIVE_ROLES:
            return True
        hay = f"{self.label} {self.title}".strip()
        return bool(_SENSITIVE_RE.search(hay))

    @property
    def is_interactive(self) -> bool:
        return self.role in INTERACTIVE_ROLES and self.has_frame

    @property
    def has_frame(self) -> bool:
        return bool(self.frame) and len(self.frame) >= 4

    def center_px(self) -> tuple[float, float] | None:
        if not self.has_frame:
            return None
        x, y, w, h = self.frame
        return x + w / 2, y + h / 2


def parse(node_tree: str | None) -> AxNode | None:
    if not node_tree:
        return None
    try:
        tree = json.loads(node_tree)
    except json.JSONDecodeError as e:
        log.warning("ax parse failed: %s", e)
        return None
    return _from_dict(tree)


def _from_dict(d: dict) -> AxNode:
    frame = d.get("frame") or d.get("position") or d.get("ax_frame")
    frame = [float(v) for v in frame] if frame and len(frame) >= 4 else None
    node = AxNode(
        role=str(d.get("role") or "").lower(),
        label=str(d.get("label") or ""),
        title=str(d.get("title") or ""),
        frame=frame,
        identifier=d.get("identifier") or d.get("ax_identifier"),
    )
    for child in d.get("children") or []:
        node.children.append(_from_dict(child))
    return node


def walk(root: AxNode | None) -> list[AxNode]:
    """Iterative pre-order traversal, capped at MAX_NODES."""
    if root is None:
        return []
    out: list[AxNode] = []
    stack: list[AxNode] = [root]
    while stack:
        n = stack.pop()
        out.append(n)
        if len(out) >= MAX_NODES:
            log.warning("ax walk capped at %d nodes", MAX_NODES)
            break
        for c in reversed(n.children):
            stack.append(c)
    return out


def find_by_label(root: AxNode | None, query: str, mode: str = "substring") -> AxNode | None:
    """Find first node matching query in label/title. mode: exact|prefix|substring."""
    q = query.lower().strip()
    if not q:
        return None
    for n in walk(root):
        hay = f"{n.label} {n.title}".strip().lower()
        if mode == "exact" and hay == q:
            return n
        if mode == "prefix" and hay.startswith(q):
            return n
        if mode == "substring" and q in hay:
            return n
    return None


def find_by_role(root: AxNode | None, query: str, role_hint: str) -> AxNode | None:
    """Find node whose role matches role_hint and label contains query (substring)."""
    q = query.lower().strip()
    hint = role_hint.lower().strip()
    if not q:
        return None
    for n in walk(root):
        if hint and n.role != hint:
            continue
        hay = f"{n.label} {n.title}".strip().lower()
        if q in hay:
            return n
    return None


def collect_interactive(root: AxNode | None, max_nodes: int = 40) -> list[AxNode]:
    out: list[AxNode] = []
    for n in walk(root):
        if len(out) >= max_nodes:
            break
        if n.is_interactive:
            out.append(n)
    return out


def collect_sensitive(root: AxNode | None, max_nodes: int = 200) -> list[AxNode]:
    # E6: symmetric with collect_interactive's early-stop. Sensitive nodes are
    # few, but cap the collected set so a pathological tree cannot grow the
    # blackout pass unbounded (walk() itself is already MAX_NODES-capped).
    out: list[AxNode] = []
    for n in walk(root):
        if len(out) >= max_nodes:
            log.warning("collect_sensitive capped at %d nodes", max_nodes)
            break
        if n.is_sensitive and n.has_frame:
            out.append(n)
    return out


def guess_role(query: str) -> str:
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


def strip_sensitive_labels(root: AxNode | None, max_depth: int = 256) -> AxNode | None:
    """Return a copy of the tree with sensitive node labels redacted (kept as
    '***' so the model still sees a field exists, but never the value/text).

    R5: iterative copy with a depth cap so a deeply-nested Electron/Web AX tree
    cannot blow the Python recursion limit (default 1000) and crash the
    fail-closed masking path itself.
    """
    if root is None:
        return None
    new_root = AxNode(
        role=root.role,
        label="***" if root.is_sensitive else root.label,
        title="***" if root.is_sensitive else root.title,
        frame=list(root.frame) if root.frame else None,
        identifier=root.identifier,
    )
    # stack of (source_node, target_parent, depth)
    stack: list[tuple[AxNode, AxNode, int]] = [(root, new_root, 0)]
    while stack:
        src, parent, depth = stack.pop()
        if depth >= max_depth:
            log.warning("strip_sensitive_labels capped at depth %d", max_depth)
            continue
        for c in src.children:
            child = AxNode(
                role=c.role,
                label="***" if c.is_sensitive else c.label,
                title="***" if c.is_sensitive else c.title,
                frame=list(c.frame) if c.frame else None,
                identifier=c.identifier,
            )
            parent.children.append(child)
            stack.append((c, child, depth + 1))
    return new_root
