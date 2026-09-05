"""fusion-osagent CLI: preflight + one-shot actions.

Subcommands:
  preflight        software self-check (pattern migrated from fusion-robot d1_preflight)
  screenshot       capture one frame, write png to --out
  click            click at (x,y) logical points
  health           ping mlx + executor
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

from fusion_core import get_logger

from os_agent.api import DesktopAgent
from os_agent.config import OsaConfig

log = get_logger("os_agent.cli")


def preflight() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}: {detail[:200]}")

    try:
        import fusion_core
        check("fusion-core import", True, getattr(fusion_core, "__file__", "?"))
    except Exception as e:
        check("fusion-core import", False, str(e))

    try:
        import fusion_executor  # noqa: F401

        check("fusion-executor import", True, getattr(fusion_executor, "__file__", "ok"))
    except Exception as e:
        check("fusion-executor import", False, str(e))

    sock = Path(OsaConfig().executor_sock)
    check("executor sock path", True, str(sock))

    mlx_url = OsaConfig().fusion_mlx_url
    check("fusion-mlx url", True, mlx_url)

    model = OsaConfig().fast_model
    check("fast model", True, model)

    cache = Path.home() / ".fusion-mlx" / "models"
    check("mlx model cache dir", cache.is_dir(), str(cache))

    ok_count = sum(1 for _, ok, _ in results if ok)
    fail = len(results) - ok_count
    print(f"\n{'='*50}\npreflight: {ok_count} ok / {fail} fail / {len(results)} total")
    return 0 if fail == 0 else 1


async def _screenshot(out: str, stub: bool) -> int:
    cfg = OsaConfig(stub_mode=stub)
    agent = DesktopAgent(cfg)
    try:
        shot = await agent.screenshot()
        if not shot.png_b64:
            print("screenshot: no image returned")
            return 1
        Path(out).write_bytes(base64.b64decode(shot.png_b64))
        print(f"screenshot: wrote {out} ({shot.width}x{shot.height} scale={shot.scale_factor})")
        return 0
    finally:
        await agent.close()


async def _click(x: float, y: float, stub: bool) -> int:
    cfg = OsaConfig(stub_mode=stub)
    agent = DesktopAgent(cfg)
    try:
        res = await agent.click(x, y)
        print(f"click: ok={res.ok} latency={res.latency_ms}ms err={res.error}")
        return 0 if res.ok else 1
    finally:
        await agent.close()


async def _health(stub: bool) -> int:
    cfg = OsaConfig(stub_mode=stub)
    agent = DesktopAgent(cfg)
    try:
        ok = await agent.mlx.health()
        print(f"mlx health: {'✅' if ok else '❌'}")
        return 0 if ok else 1
    finally:
        await agent.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="fusion-osagent", description="Desktop Embodied AI barrier layer")
    ap.add_argument("--stub", action="store_true", help="use stub adapters (offline)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="software self-check")
    p_shot = sub.add_parser("screenshot", help="capture one frame")
    p_shot.add_argument("--out", default="osa_screenshot.png")
    p_click = sub.add_parser("click", help="click at logical point (x,y)")
    p_click.add_argument("x", type=float)
    p_click.add_argument("y", type=float)
    sub.add_parser("health", help="ping mlx + executor")
    args = ap.parse_args()

    if args.cmd == "preflight":
        sys.exit(preflight())
    if args.cmd == "screenshot":
        sys.exit(asyncio.run(_screenshot(args.out, args.stub)))
    if args.cmd == "click":
        sys.exit(asyncio.run(_click(args.x, args.y, args.stub)))
    if args.cmd == "health":
        sys.exit(asyncio.run(_health(args.stub)))


if __name__ == "__main__":
    main()
