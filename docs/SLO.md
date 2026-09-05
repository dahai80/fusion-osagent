# SLO & Capacity Baseline — fusion-osagent

> Target audience: ops / SRE running fusion-osagent in controlled enterprise
> production. This is the formal SLO + capacity baseline referenced by the 0905
> product-readiness audit (GA ops gap 3). It pairs with the Prometheus scrape
> endpoint (`serve-metrics`) and the audit-log rotation/retention knobs.

## 1. Service definition

fusion-osagent is a desktop embodied-AI barrier layer: it turns a high-level
intent ("click the Submit button") into a perception → mask → decide → act →
assert loop on a local macOS desktop, backed by a local MLX VL model. It is NOT
a public-facing service — it runs on operator workstations or controlled nodes,
bound to localhost.

- **Tier**: internal platform (controlled enterprise production).
- **Surface**: Python library `DesktopAgent` + `fusion-osagent` CLI + Prometheus
  scrape endpoint on `127.0.0.1:9100/metrics`.
- **Dependencies**: fusion-mlx (local VL inference, `localhost:11434`),
  fusion-executor (UDS), fusion-browser (UDS), macOS Accessibility + Screen
  Recording TCC grants.

## 2. Latency SLOs

Per-step budgets (one decide→act→assert cycle). Measured via the
`osagent_action_*_latency_ms` histograms scraped from `/metrics`.

| Stage | Target (p50) | Target (p95) | Target (p99) | Hard ceiling |
|-------|-------------|-------------|-------------|--------------|
| Fast-core decide (Qwen2.5-VL-7B) | 400 ms | 900 ms | 1500 ms | 5000 ms (`step_timeout_ms`) |
| Slow-core decide (Qwen2.5-VL-32B, arbitration) | 1500 ms | 3500 ms | 6000 ms | 15000 ms (`inspect_timeout_ms`) |
| Mask (sha1-keyed, cached) | 5 ms | 20 ms | 50 ms | — |
| Action dispatch (click/type/key) | 30 ms | 100 ms | 200 ms | — |
| Assert (frame diff) | 40 ms | 120 ms | 250 ms | — |
| Full step end-to-end | 600 ms | 1500 ms | 3000 ms | `step_timeout_ms` |

- **Error budget**: 1% of steps over 30 days may exceed the p95 target before
  the SLO is considered breached (enterprise-internal, not 99.99% public-SaaS).
- **Trajectory move path**: bounded by `move_path_timeout_ms` (default 30000 ms)
  for the whole path, not per-point.

## 3. Availability / error SLOs

| Signal | Target | Source metric |
|--------|--------|---------------|
| Step success rate | ≥ 99.0% over 24h | `osagent_action_click_ok / osagent_action_click_total` |
| Circuit breaker open rate | ≤ 5% of time windows | `osagent_breaker_state` (gauge, 1=open) |
| Cluster-level mlx health | ≥ 95% of scrape windows healthy | `osagent_cluster_open` (gauge, 0=healthy) |
| VLM cache hit rate (warm) | ≥ 60% on repeat workloads | `osagent_vlm_hit_rate` |

- A step is **failed** when `assert_changed` reports no change OR the executor
  returns `ok=false` OR the step raises. Failures feed the breaker.
- The breaker opens after `breaker_failure_threshold` failures (default 5) in a
  `breaker_window_s` (default 30s) window, or a failure rate ≥
  `breaker_failure_rate` (default 0.5) with at least
  `breaker_min_calls_for_rate` (default 10) calls. While open, the agent
  short-circuits fast instead of hammering a down mlx.

## 4. Capacity baseline (Apple Silicon single node)

Reference hardware: M-series Pro/Max, 32–64 GB unified memory. MLX local
inference, no GPU offload.

| Resource | Baseline limit | Rationale |
|----------|---------------|-----------|
| Concurrent VLM inferences | 2 (`vlm_concurrency`) | VL model VRAM; beyond 2 OOMs the 7B 4-bit on 32GB |
| Image cache entries | 32 (`image_cache_max`) | frames are full screenshots; 32 ≈ a short task window |
| VLM cache TTL | 3.0s (`vlm_cache_ttl`) | UI changes fast; stale locate is worse than a re-infer |
| Heal cycles per plan | 4 (`planner_max_heal_cycles`) | bounded retry; beyond 4 a human should intervene |
| Audit log active file | 50 MB then rotate (`audit_rotate_max_bytes`) | keeps tail/scan cheap |
| Audit archives retained | 10 files / 30 days (`audit_retention_files`/`_days`) | compliance window vs disk |
| Prometheus scrape interval | 15s recommended | counters are cumulative; 15s is cheap and responsive |

### Multi-node scaling limits

fusion-osagent is **per-desktop**. Multi-node means N operator desktops each
running one agent, coordinated by the fleet registry (`cluster nodes`).
- **No shared mlx pool**: each node talks to its own local mlx. The cluster
  health aggregate is a *fan-in view*, not a load-balanced backend.
- **Scaling ceiling**: bounded by per-node mlx capacity (2 concurrent
  inferences), not by osagent itself. Adding nodes adds linear desktop
  coverage; it does NOT multiply a single desktop's throughput.
- **Registry**: heartbeat-based; a node is live if its heartbeat is fresher than
  the registry TTL. Stale nodes (SIGTERM-killed without graceful close) appear
  until TTL expiry — the P1 SIGTERM→close wiring limits this.

## 5. Alerting recommendations (Prometheus)

Scrape `fusion-osagent serve-metrics --port 9100`. Alert on:

- `osagent_breaker_state == 1` for > 1m → mlx backend degraded; page ops.
- `rate(osagent_action_click_total - osagent_action_click_ok[5m]) / rate(osagent_action_click_total[5m]) > 0.05` → step failure rate over 5%.
- `histogram_quantile(0.99, rate(osagent_action_click_latency_ms_bucket[5m])) > 3000` → p99 latency breach.
- `osagent_cluster_open == 1` for > 2m → cluster-wide mlx outage.
- `up == 0` → scrape endpoint down (agent process dead).

## 6. Operational runbook (brief)

- **Breaker stuck open**: check `fusion-mlx start.sh status` + `doctor`; restart
  mlx; breaker auto-closes after `breaker_cooldown_s` (default 15s).
- **Step timeout storms**: raise `step_timeout_ms` only if mlx is confirmed
  slow-but-healthy; otherwise the timeout is correctly protecting the loop.
- **Audit disk full**: lower `audit_retention_files` / `_days`, or raise
  `audit_rotate_max_bytes` if compliance needs longer tail. Rotation is
  best-effort and fail-open (a rotate error logs but never blocks the action).
- **Prometheus 500s**: the scrape handler returns 500 only if
  `metrics_snapshot()` raises — almost always a closed adapter on a stale agent.
  Restart the `serve-metrics` process.

## 7. SLO review cadence

Reviewed quarterly, or on any of: model swap (fast/slow VL), hardware
generation change, breaker/timeout knob defaults change, or a sustained error
budget burn. Latency targets are only valid for the **declared models**
(Qwen2.5-VL-7B/32B 4-bit on MLX); a different model tier needs a re-baseline.
