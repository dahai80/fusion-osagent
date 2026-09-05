"""fusion-osagent runtime config + coordinate primitives.

Config resolved from env (fusion-core pattern: env first, then sensible defaults).
Coordinate space: single logical-point space at the API layer; adapters convert
to/from physical pixels. scale_factor default 2.0 on Apple Silicon Retina until
fusion-executor E1 exposes a guaranteed scale_factor field.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from fusion_core import get_logger

log = get_logger("os_agent.config")


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("env %s=%r not int, fall back %d", key, raw, default)
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("env %s=%r not float, fall back %s", key, raw, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_mlx_api_key() -> str:
    raw = os.environ.get("FUSION_MLX_API_KEY")
    if raw:
        return raw
    settings = os.path.join(os.path.expanduser("~"), ".fusion-mlx", "settings.json")
    try:
        import json

        with open(settings) as fh:
            d = json.load(fh)
        key = d.get("auth", {}).get("api_key", "")
        if key:
            return key
    except Exception:
        pass
    return ""


@dataclass
class OsaConfig:
    fusion_mlx_url: str = field(default_factory=lambda: os.environ.get("FUSION_MLX_URL", "http://localhost:11434/v1"))
    fusion_mlx_api_key: str = field(default_factory=_resolve_mlx_api_key)
    fast_model: str = field(
        default_factory=lambda: os.environ.get("OSA_FAST_MODEL", "mlx-community/Qwen2.5-VL-7B-Instruct-4bit")
    )
    slow_model: str = field(
        default_factory=lambda: os.environ.get("OSA_SLOW_MODEL", "mlx-community/Qwen2.5-VL-32B-Instruct-4bit")
    )
    executor_sock: str = field(
        default_factory=lambda: os.environ.get(
            "FUSION_EXECUTOR_SOCK", os.path.join(os.path.expanduser("~"), ".fusion-executor", "fe.sock")
        )
    )
    browser_sock: str = field(
        default_factory=lambda: os.environ.get(
            "OSA_BROWSER_SOCK", os.path.join(os.path.expanduser("~"), ".fusion-browser", "fb.sock")
        )
    )
    agent_studio_url: str = field(
        default_factory=lambda: os.environ.get("OSA_AGENT_STUDIO_URL", "http://localhost:11440")
    )
    scale_factor: float = field(default_factory=lambda: _env_float("OSA_SCALE_FACTOR", 2.0))
    step_timeout_ms: int = field(default_factory=lambda: _env_int("OSA_STEP_TIMEOUT_MS", 5000))
    step_latency_target_ms: int = field(default_factory=lambda: _env_int("OSA_LATENCY_TARGET_MS", 150))
    move_path_timeout_ms: int = field(
        default_factory=lambda: _env_int("OSA_MOVE_PATH_TIMEOUT_MS", 30000)
    )  # R3: whole-path budget
    assert_diff_threshold: float = field(default_factory=lambda: _env_float("OSA_ASSERT_DIFF_THRESHOLD", 0.002))  # R4
    vlm_concurrency: int = field(default_factory=lambda: _env_int("OSA_VLM_CONCURRENCY", 2))  # A4
    image_cache_max_entries: int = field(default_factory=lambda: _env_int("OSA_IMAGE_CACHE_MAX", 32))  # A5
    planner_max_heal_cycles: int = field(default_factory=lambda: _env_int("OSA_PLANNER_MAX_HEAL_CYCLES", 4))  # A2
    stub_mode: bool = field(default_factory=lambda: _env_bool("OSA_STUB_MODE", False))
    dual_track_arbitrate: bool = field(default_factory=lambda: _env_bool("OSA_DUAL_TRACK_ARBITRATE", True))
    fast_confidence_floor: float = field(default_factory=lambda: _env_float("OSA_FAST_CONF_FLOOR", 0.5))
    vlm_cache_ttl: float = field(default_factory=lambda: _env_float("OSA_VLM_CACHE_TTL", 3.0))  # seconds; 0 disables
    vlm_timeout: float = field(
        default_factory=lambda: _env_float("OSA_VLM_TIMEOUT", 60.0)
    )  # seconds; A2 bound on a single mlx inference
    inspect_timeout_ms: int = field(
        default_factory=lambda: _env_int("OSA_INSPECT_TIMEOUT_MS", 15000)
    )  # E5: AX tree walk needs longer than a click
    trajectory_seed: int | None = field(
        default_factory=lambda: _env_int("OSA_TRAJECTORY_SEED", 0) or None
    )  # N10: None = per-target derived seed
    # Gap 3: circuit breaker knobs for the mlx cluster.
    breaker_failure_threshold: int = field(default_factory=lambda: _env_int("OSA_BREAKER_FAILURE_THRESHOLD", 5))
    breaker_window_s: float = field(default_factory=lambda: _env_float("OSA_BREAKER_WINDOW_S", 30.0))
    breaker_failure_rate: float = field(default_factory=lambda: _env_float("OSA_BREAKER_FAILURE_RATE", 0.5))
    breaker_cooldown_s: float = field(default_factory=lambda: _env_float("OSA_BREAKER_COOLDOWN_S", 15.0))
    breaker_min_calls_for_rate: int = field(default_factory=lambda: _env_int("OSA_BREAKER_MIN_CALLS_FOR_RATE", 10))

    def __post_init__(self) -> None:
        # P1 fix: clamp/validate env-driven knobs so a misconfigured negative
        # value cannot silently invert behavior (e.g. breaker_failure_threshold
        # <= 0 opens on the first call; breaker_cooldown_s < 0 makes the breaker
        # never stay open; vlm_concurrency < 1 deadlocks the semaphore).
        c = self
        if c.vlm_concurrency < 1:
            log.warning("vlm_concurrency=%d clamped to 1", c.vlm_concurrency)
            c.vlm_concurrency = 1
        if c.image_cache_max_entries < 1:
            c.image_cache_max_entries = 1
        if c.planner_max_heal_cycles < 0:
            c.planner_max_heal_cycles = 0
        if c.step_timeout_ms <= 0:
            log.warning("step_timeout_ms=%d clamped to 5000", c.step_timeout_ms)
            c.step_timeout_ms = 5000
        if c.vlm_timeout <= 0:
            log.warning("vlm_timeout=%s clamped to 60.0", c.vlm_timeout)
            c.vlm_timeout = 60.0
        if c.assert_diff_threshold < 0:
            c.assert_diff_threshold = 0.0
        if c.fast_confidence_floor < 0:
            c.fast_confidence_floor = 0.0
        elif c.fast_confidence_floor > 1:
            c.fast_confidence_floor = 1.0
        if c.breaker_failure_threshold < 1:
            log.warning("breaker_failure_threshold=%d clamped to 1", c.breaker_failure_threshold)
            c.breaker_failure_threshold = 1
        if c.breaker_window_s <= 0:
            log.warning("breaker_window_s=%s clamped to 30.0", c.breaker_window_s)
            c.breaker_window_s = 30.0
        if c.breaker_cooldown_s < 0:
            log.warning("breaker_cooldown_s=%s clamped to 0.0", c.breaker_cooldown_s)
            c.breaker_cooldown_s = 0.0
        if c.breaker_failure_rate < 0:
            c.breaker_failure_rate = 0.0
        elif c.breaker_failure_rate > 1:
            c.breaker_failure_rate = 1.0
        if c.breaker_min_calls_for_rate < 1:
            c.breaker_min_calls_for_rate = 1
        if c.scale_factor <= 0:
            log.warning("scale_factor=%s clamped to 2.0", c.scale_factor)
            c.scale_factor = 2.0


def points_to_pixels(x: float, y: float, scale: float) -> tuple[float, float]:
    return x * scale, y * scale


def pixels_to_points(px: float, py: float, scale: float) -> tuple[float, float]:
    if scale == 0:
        log.warning("scale_factor=0, treat as 1.0")
        scale = 1.0
    return px / scale, py / scale
