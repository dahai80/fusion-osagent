"""Human-like trajectory tests (Phase 2.2)."""
from __future__ import annotations

from os_agent.trajectory import TrajectoryConfig, bezier_path, key_jitter_ms


def test_bezier_endpoints_exact():
    pts = bezier_path((0.0, 0.0), (100.0, 50.0), TrajectoryConfig(steps=20, jitter=0.0, seed=1))
    assert pts[0] == (0.0, 0.0)
    assert pts[-1] == (100.0, 50.0)
    assert len(pts) == 21


def test_bezier_not_straight_line():
    pts = bezier_path((0.0, 0.0), (200.0, 0.0), TrajectoryConfig(steps=30, jitter=0.0, seed=2))
    # with a perpendicular bow, midpoints must leave the y=0 line
    mid = pts[15]
    assert mid[1] != 0.0


def test_bezier_seed_reproducible():
    cfg = TrajectoryConfig(steps=10, jitter=2.0, seed=42)
    a = bezier_path((10.0, 10.0), (90.0, 80.0), cfg)
    b = bezier_path((10.0, 10.0), (90.0, 80.0), cfg)
    assert a == b


def test_bezier_zero_length():
    pts = bezier_path((50.0, 50.0), (50.0, 50.0), TrajectoryConfig(steps=5, jitter=0.0, seed=1))
    assert all(p == (50.0, 50.0) for p in pts)


def test_key_jitter_nonnegative_and_bounded():
    cfg = TrajectoryConfig(seed=7)
    vals = [key_jitter_ms(20, cfg) for _ in range(20)]
    assert all(v >= 0 for v in vals)
    assert max(vals) <= 32 and min(vals) >= 12
