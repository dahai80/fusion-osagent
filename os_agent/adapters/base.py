"""Adapter base types: uniform contracts across siblings.

Locator: an element target (AX semantic, coordinate, or visual query).
Screenshot: captured frame + dims + scale, the single coordinate-space carrier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from fusion_core import get_logger

log = get_logger("os_agent.adapters.base")


@dataclass
class Locator:
    kind: Literal["point", "ax", "visual"]
    x: float | None = None
    y: float | None = None
    ax_role: str | None = None
    ax_label: str | None = None
    ax_identifier: str | None = None
    visual_query: str | None = None
    raw: dict = field(default_factory=dict)

    def as_point(self) -> tuple[float, float]:
        if self.x is None or self.y is None:
            raise ValueError(f"locator {self.kind} has no coordinates")
        return self.x, self.y


@dataclass
class Screenshot:
    png_b64: str | None
    width: int | None
    height: int | None
    scale_factor: float = 1.0
    node_tree: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def has_ax(self) -> bool:
        return bool(self.node_tree)


@runtime_checkable
class Adapter(Protocol):
    name: str

    async def screenshot(self) -> Screenshot: ...

    async def close(self) -> None: ...
