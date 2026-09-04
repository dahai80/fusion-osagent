"""Human-like mouse trajectories (PRD F3.1 / Phase 2.2).

Cubic Bezier interpolation + per-point jitter between a start and end point,
producing a list of waypoints the executor can step through. Wraps CGEvent
move semantics on top of the executor without modifying it (downstream issues
E2 batch-move will consume the full list; today callers can iterate).

Deterministic given the seed (Rule 5): no Math.random at module level; the
jitter RNG is seeded so replays reproduce the same path.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from fusion_core import get_logger

log = get_logger("os_agent.trajectory")

DEFAULT_STEPS = 24
DEFAULT_JITTER = 1.5  # logical points of perpendicular noise


@dataclass
class TrajectoryConfig:
    steps: int = DEFAULT_STEPS
    jitter: float = DEFAULT_JITTER
    seed: int | None = None


def bezier_path(start: tuple[float, float], end: tuple[float, float], cfg: TrajectoryConfig | None = None) -> list[tuple[float, float]]:
    """Cubic Bezier from start to end with two auto-control points + jitter.

    Control points are placed along the straight line with a perpendicular
    bow so the path is not a straight line (more human). Returns `cfg.steps`
    waypoints inclusive of start (t=0) and end (t=1).
    """
    cfg = cfg or TrajectoryConfig()
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    # perpendicular unit vector for the bow
    if length == 0:
        nx, ny = 0.0, 0.0
    else:
        nx, ny = -dy / length, dx / length
    bow = min(length * 0.15, 60.0)
    # two control points at t=1/3 and t=2/3, offset perpendicular
    c1 = (x0 + dx / 3 + nx * bow, y0 + dy / 3 + ny * bow)
    c2 = (x0 + 2 * dx / 3 + nx * bow * 0.5, y0 + 2 * dy / 3 + ny * bow * 0.5)
    rng = random.Random(cfg.seed)
    pts: list[tuple[float, float]] = []
    for i in range(cfg.steps + 1):
        t = i / cfg.steps
        bx = (1 - t) ** 3 * x0 + 3 * (1 - t) ** 2 * t * c1[0] + 3 * (1 - t) * t ** 2 * c2[0] + t ** 3 * x1
        by = (1 - t) ** 3 * y0 + 3 * (1 - t) ** 2 * t * c1[1] + 3 * (1 - t) * t ** 2 * c2[1] + t ** 3 * y1
        # jitter shrinks toward the endpoint so the final click lands exactly
        scale = (1 - t) * cfg.jitter
        bx += rng.uniform(-scale, scale)
        by += rng.uniform(-scale, scale)
        pts.append((round(bx, 2), round(by, 2)))
    # guarantee exact endpoint landing
    pts[-1] = (x1, y1)
    log.info("bezier path: %d pts start=%s end=%s bow=%.1f", len(pts), start, end, bow)
    return pts


def key_jitter_ms(base_ms: int, cfg: TrajectoryConfig | None = None) -> int:
    """Small random key-press delay around a base, seeded for reproducibility."""
    cfg = cfg or TrajectoryConfig()
    rng = random.Random(cfg.seed)
    delta = rng.randint(-8, 12)
    return max(0, base_ms + delta)
