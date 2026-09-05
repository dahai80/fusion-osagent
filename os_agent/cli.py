"""fusion-osagent CLI: preflight + one-shot actions + ops surface.

Subcommands:
  preflight        software self-check (pattern migrated from fusion-robot d1_preflight)
  screenshot       capture one frame, write png to --out
  click            click at (x,y) logical points
  health           ping mlx + executor + browser (aggregate)
  metrics          dump metrics_snapshot() as JSON (counters/histograms/breaker/cluster)
  audit query      query the persisted audit JSONL (--kind, --since, --last)
  cluster nodes    list live osagent nodes in the fleet registry
  cluster health   show cluster-level mlx health (aggregate failures, open?)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import signal
import sys
from pathlib import Path

from fusion_core import get_logger

from os_agent.api import DesktopAgent
from os_agent.config import OsaConfig

log = get_logger("os_agent.cli")


async def _mlx_health_ping(cfg: OsaConfig) -> bool:
    """E2: live mlx health check for the (otherwise synchronous) preflight."""
    from os_agent.adapters.mlx import MlxAdapter

    mlx = MlxAdapter(cfg)
    try:
        return await mlx.health()
    finally:
        await mlx.close()


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

    # E2: a path/url string check always reports ✅ even when mlx is down, so
    # ops pass preflight and only discover the breakage when every click fails.
    # Actually ping the mlx health endpoint so a stopped engine fails loudly.
    try:
        mlx_ok = asyncio.run(_mlx_health_ping(OsaConfig()))
        check(
            "fusion-mlx reachable (health ping)", mlx_ok, mlx_url if mlx_ok else "health ping failed — is mlx running?"
        )
    except Exception as e:
        check("fusion-mlx reachable (health ping)", False, str(e))

    ok_count = sum(1 for _, ok, _ in results if ok)
    fail = len(results) - ok_count
    print(f"\n{'=' * 50}\npreflight: {ok_count} ok / {fail} fail / {len(results)} total")
    return 0 if fail == 0 else 1


def _install_sigterm_close(agent: DesktopAgent) -> None:
    """P1 fix: wire SIGTERM → graceful close so a killed agent deregisters
    from the fleet registry and closes adapters instead of leaving stale
    nodes + leaked UDS sessions."""
    loop = asyncio.get_event_loop()

    def _handler(*_):
        log.warning("SIGTERM received — graceful close")
        asyncio.create_task(agent.close())

    try:
        loop.add_signal_handler(signal.SIGTERM, _handler)
    except (NotImplementedError, RuntimeError):
        # add_signal_handler unavailable (e.g. non-main thread / Windows)
        signal.signal(signal.SIGTERM, lambda *_: None)


async def _screenshot(out: str, stub: bool) -> int:
    cfg = OsaConfig(stub_mode=stub)
    agent = DesktopAgent(cfg)
    _install_sigterm_close(agent)
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
    _install_sigterm_close(agent)
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
        results = await agent.health()
        for name, ok in results.items():
            if name == "ok":
                continue
            print(f"{name} health: {'✅' if ok else '❌'}")
        print(f"overall: {'✅' if results['ok'] else '❌'}")
        return 0 if results["ok"] else 1
    finally:
        await agent.close()


async def _metrics(stub: bool) -> int:
    # metrics are meaningful in stub too (counters/histograms populate in any
    # run); but a fresh agent has no activity — still emit the schema so an
    # exporter can discover the shape.
    cfg = OsaConfig(stub_mode=stub)
    agent = DesktopAgent(cfg)
    try:
        print(json.dumps(agent.metrics_snapshot(), ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        await agent.close()


def _audit_query(kind: str | None, since: float | None, last: int | None, path: str | None) -> int:
    from os_agent.audit_log import AuditLog, default_path

    audit_path = path or default_path()
    if not Path(audit_path).is_file():
        print(f"audit: no persisted log at {audit_path} (set OSA_AUDIT_PATH to enable persistence)")
        return 1
    # query_disk reads the persisted JSONL (query() is in-memory only).
    audit = AuditLog(path=audit_path)
    entries = [
        {"ts": e.ts, "agent_id": e.agent_id, "kind": e.kind, "detail": e.detail}
        for e in audit.query_disk(kind=kind, since=since)
    ]
    if last:
        entries = entries[-last:]
    print(json.dumps(entries, ensure_ascii=False, indent=2, default=str))
    print(f"audit: {len(entries)} record(s) from {audit_path}")
    return 0


async def _cluster_nodes() -> int:
    from os_agent.coordination import NodeRegistry

    reg = NodeRegistry()
    nodes = reg.live_nodes()
    out = [
        {
            "node_id": n.node_id,
            "started_at": n.started_at,
            "last_heartbeat": n.last_heartbeat,
            "meta": n.meta,
        }
        for n in nodes
    ]
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"cluster: {len(nodes)} live node(s)")
    return 0


async def _cluster_health() -> int:
    from os_agent.coordination import build_cluster_health

    ch = build_cluster_health()
    try:
        snap = ch.snapshot()
    except OSError as e:
        print(f"cluster health: read failed: {e}")
        return 1
    print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))
    return 0


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
    sub.add_parser("health", help="ping mlx + executor + browser (aggregate)")
    sub.add_parser("metrics", help="dump metrics snapshot as JSON")
    p_audit = sub.add_parser("audit", help="query the audit trail")
    p_audit_sub = p_audit.add_subparsers(dest="audit_cmd", required=True)
    p_q = p_audit_sub.add_parser("query", help="query persisted audit JSONL")
    p_q.add_argument("--kind", default=None, help="filter by kind (decide/action/assert/heal/replay)")
    p_q.add_argument("--since", type=float, default=None, help="unix timestamp lower bound")
    p_q.add_argument("--last", type=int, default=None, help="only the last N records")
    p_q.add_argument("--path", default=None, help="audit JSONL path (default OSA_AUDIT_PATH)")
    p_cluster = sub.add_parser("cluster", help="fleet coordination")
    p_cluster_sub = p_cluster.add_subparsers(dest="cluster_cmd", required=True)
    p_cluster_sub.add_parser("nodes", help="list live osagent nodes")
    p_cluster_sub.add_parser("health", help="cluster-level mlx health")
    args = ap.parse_args()

    if args.cmd == "preflight":
        sys.exit(preflight())
    if args.cmd == "screenshot":
        sys.exit(asyncio.run(_screenshot(args.out, args.stub)))
    if args.cmd == "click":
        sys.exit(asyncio.run(_click(args.x, args.y, args.stub)))
    if args.cmd == "health":
        sys.exit(asyncio.run(_health(args.stub)))
    if args.cmd == "metrics":
        sys.exit(asyncio.run(_metrics(args.stub)))
    if args.cmd == "audit":
        if args.audit_cmd == "query":
            sys.exit(_audit_query(args.kind, args.since, args.last, args.path))
    if args.cmd == "cluster":
        if args.cluster_cmd == "nodes":
            sys.exit(asyncio.run(_cluster_nodes()))
        if args.cluster_cmd == "health":
            sys.exit(asyncio.run(_cluster_health()))


if __name__ == "__main__":
    main()
