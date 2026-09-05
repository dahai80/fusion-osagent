"""Prometheus exposition format exporter (GA ops gap 1).

metrics_snapshot() returns a plain dict; Prometheus scrapes the text-based
exposition format over HTTP. This module converts a snapshot dict into the
Prometheus 0.0.4 text format so an external prometheus.yml can scrape
`fusion-osagent serve-metrics --port 9100` without a prometheus_client
dependency (local-first, zero-dep).

Format rules (https://prometheus.io/docs/instrumenting/exposition_formats/):
- `# HELP <name> <text>`  human help
- `# TYPE <name> counter|gauge|histogram`
- `<name>{<labels>} <value>`  one sample per line
- Counters are monotonic; gauges can move; histograms emit `<name>_bucket{le=}`,
  `<name>_sum`, `<name>_count`.

We map the snapshot generically:
- counters dict  -> counter
- scalar numbers -> gauge
- cache stats    -> gauge (hits/misses/hit_rate/total)
- histograms     -> histogram (_bucket / _sum / _count)
- breaker/cluster dicts -> gauges for their boolean/numeric fields
"""

from __future__ import annotations

from typing import Any

from fusion_core import get_logger

log = get_logger("os_agent.prometheus")

_PREFIX = "osagent"


def _fmt_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{str(v)}"' for k, v in labels.items()]
    return "{" + ",".join(parts) + "}"


def _sanitize(part: str) -> str:
    # Prometheus metric names must match [a-zA-Z_:][a-zA-Z0-9_:]*. Snapshot
    # keys carry dots/dashes (action.click.total) -> collapse to underscores.
    return part.replace(".", "_").replace("-", "_")


def _name(*parts: str) -> str:
    return "_".join([_PREFIX, *[_sanitize(p) for p in parts]])


def render_prometheus(snapshot: dict[str, Any]) -> str:
    """Convert a metrics_snapshot() dict into Prometheus text exposition format."""
    lines: list[str] = []

    # counters
    counters = snapshot.get("counters", {}) or {}
    if isinstance(counters, dict):
        for cname, val in sorted(counters.items()):
            metric = _name(cname)
            lines.append(f"# HELP {metric} counter {cname}")
            lines.append(f"# TYPE {metric} counter")
            lines.append(f"{metric} {val}")

    # histograms
    histograms = snapshot.get("histograms", {}) or {}
    if isinstance(histograms, dict):
        for hname, h in sorted(histograms.items()):
            if not isinstance(h, dict):
                continue
            metric = _name(hname)
            lines.append(f"# HELP {metric} latency histogram for {hname} (ms)")
            lines.append(f"# TYPE {metric} histogram")
            buckets = h.get("buckets") or []
            for label, count in buckets:
                # label looks like "<= 25"
                le = label.replace("<= ", "").strip()
                lines.append(f'{metric}_bucket{{le="{le}"}} {count}')
            lines.append(f'{metric}_bucket{{le="+Inf"}} {h.get("count", 0)}')
            lines.append(f"{metric}_sum {h.get('sum_ms', 0)}")
            lines.append(f"{metric}_count {h.get('count', 0)}")

    # caches
    caches = snapshot.get("caches", {}) or {}
    if isinstance(caches, dict):
        for cname, c in sorted(caches.items()):
            if not isinstance(c, dict):
                continue
            for field in ("hits", "misses", "total"):
                metric = _name(cname, field)
                lines.append(f"# HELP {metric} cache {cname} {field}")
                lines.append(f"# TYPE {metric} gauge")
                lines.append(f"{metric} {c.get(field, 0)}")
            metric = _name(cname, "hit_rate")
            lines.append(f"# HELP {metric} cache {cname} hit_rate")
            lines.append(f"# TYPE {metric} gauge")
            lines.append(f"{metric} {c.get('hit_rate', 0)}")

    # scalar top-level gauges (masker_masked_total, coordination_enabled)
    for key, val in sorted(snapshot.items()):
        if key in ("counters", "histograms", "caches"):
            continue
        if isinstance(val, (int, float)):
            metric = _name(key)
            lines.append(f"# HELP {metric} {key}")
            lines.append(f"# TYPE {metric} gauge")
            lines.append(f"{metric} {val}")

    # breaker dict -> gauges
    breaker = snapshot.get("breaker")
    if isinstance(breaker, dict):
        state = breaker.get("state", "")
        state_num = {"CLOSED": 0, "OPEN": 1, "HALF_OPEN": 2}.get(str(state), -1)
        metric = _name("breaker_state")
        lines.append(f"# HELP {metric} circuit breaker state (0=closed,1=open,2=half)")
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric} {state_num}")
        for field in ("failures", "opened_at", "cooldown_s", "failure_threshold"):
            if field in breaker:
                metric = _name("breaker", field)
                lines.append(f"# TYPE {metric} gauge")
                lines.append(f"{metric} {breaker[field]}")

    # cluster_health dict -> gauges
    cluster = snapshot.get("cluster_health")
    if isinstance(cluster, dict) and "error" not in cluster:
        for field in ("failures", "window_s", "open_threshold"):
            if field in cluster:
                metric = _name("cluster", field)
                lines.append(f"# TYPE {metric} gauge")
                lines.append(f"{metric} {cluster[field]}")
        open_val = 1 if cluster.get("open") else 0
        metric = _name("cluster_open")
        lines.append(f"# HELP {metric} cluster-level breaker open (1=open,0=closed)")
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric} {open_val}")

    return "\n".join(lines) + "\n"


async def serve_metrics(agent, host: str = "127.0.0.1", port: int = 9100) -> None:
    """Run a minimal HTTP server exposing /metrics in Prometheus text format.

    Single endpoint: GET /metrics -> render_prometheus(agent.metrics_snapshot()).
    Anything else -> 404. Stays up until cancelled/killed. Binds to localhost
    by default so the scrape endpoint is not exposed externally without an
    explicit --host 0.0.0.0. Uses the stdlib asyncio start_server (zero extra
    dependency — local-first, no aiohttp/prometheus_client needed).
    """
    import asyncio

    async def handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            writer.close()
            return
        except Exception:
            writer.close()
            return
        try:
            body = render_prometheus(agent.metrics_snapshot())
            payload = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
                f"Content-Length: {len(body.encode('utf-8'))}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode() + body.encode("utf-8")
            writer.write(payload)
            await writer.drain()
        except Exception as e:
            log.error("metrics scrape failed: %s", e)
            try:
                writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle_conn, host, port)
    log.info("prometheus metrics endpoint: http://%s:%d/metrics", host, port)
    try:
        async with server:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
