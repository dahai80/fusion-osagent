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

        d = json.load(open(settings))
        key = d.get("auth", {}).get("api_key", "")
        if key:
            return key
    except Exception:
        pass
    return ""


@dataclass
class OsaConfig:
    fusion_mlx_url: str = field(
        default_factory=lambda: os.environ.get("FUSION_MLX_URL", "http://localhost:11434/v1")
    )
    fusion_mlx_api_key: str = field(
        default_factory=_resolve_mlx_api_key
    )
    fast_model: str = field(
        default_factory=lambda: os.environ.get(
            "OSA_FAST_MODEL", "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"
        )
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
    step_timeout_ms: int = field(default_factory=lambda: _env_int("OSA_STEP_TIMEOUT_MS", 200))
    step_latency_target_ms: int = field(default_factory=lambda: _env_int("OSA_LATENCY_TARGET_MS", 150))
    stub_mode: bool = field(default_factory=lambda: _env_bool("OSA_STUB_MODE", False))
    dual_track_arbitrate: bool = field(default_factory=lambda: _env_bool("OSA_DUAL_TRACK_ARBITRATE", True))


def points_to_pixels(x: float, y: float, scale: float) -> tuple[float, float]:
    return x * scale, y * scale


def pixels_to_points(px: float, py: float, scale: float) -> tuple[float, float]:
    if scale == 0:
        log.warning("scale_factor=0, treat as 1.0")
        scale = 1.0
    return px / scale, py / scale
