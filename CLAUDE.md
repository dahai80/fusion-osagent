# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

fusion-osagent is the desktop embodied-AI barrier layer of the Fusion ecosystem: pixel-level macOS GUI automation for Apple Silicon. It is the desktop counterpart to `fusion-robot` (voxel-level robotics). Package name is `os_agent` (not `fusion_osagent`). Python ≥3.12, single package, no service port of its own — it drives sibling services and is driven programmatically / via CLI.

**Hard scope rule**: gaps in sibling repos (`fusion-executor`, `fusion-mlx`, `fusion-browser`, `fusion-agent-studio`, `fusion-code`, `fusion-autotest`) are filed as upstream GitHub issues, never patched here. See "Upstream dependencies" in README.md for the open blockers (E1, E2, B2, C1, AT1).

## Setup & Commands

```bash
cd /Users/dahai/fusion && source .venv/bin/activate   # REQUIRED, shared monorepo venv
pip install -e "fusion-osagent[test]"

pytest tests/ -v                                        # all offline/stub tests
pytest tests/test_api.py::test_click_logs_and_returns_ok -v   # single test
OSA_RUN_INTEGRATION=1 pytest -m integration -v          # live-sibling tests (needs real fusion-mlx + VL model + Accessibility TCC)
ruff check . && ruff format .                           # lint/format, line-length 120, target py312
```

- `asyncio_mode=auto`, `testpaths=["tests"]`, `--strict-markers`; only marker is `integration`.
- Default tests run fully offline via `OsaConfig(stub_mode=True)` / `OSA_STUB_MODE=1` — no live siblings needed. Keep new tests stub-based; only gate on `integration` if they truly need live services.
- New regression tests for audit fixes go into `tests/test_audit0905.py` (established pattern).

CLI (`fusion-osagent`, entry `os_agent.cli:main`):

```bash
fusion-osagent preflight                     # offline self-check
fusion-osagent --stub screenshot --out f.png # stub mode; real mode needs fusion-executor
fusion-osagent health                        # ping mlx + executor + browser
fusion-osagent metrics                       # metrics snapshot as JSON
fusion-osagent audit query [--kind K] [--since T]
fusion-osagent cluster nodes|health          # fleet coordination state
fusion-osagent serve-metrics --port 9100     # Prometheus scrape endpoint (GET /metrics)
```

## Architecture

Single entry point: `DesktopAgent` (`os_agent/api.py`). Everything flows through it.

```
DesktopAgent (api.py)
  ├─ Perception (perception.py)     dual-track locate: AX tree first, visual grounding fallback
  ├─ Reasoning (reasoning.py)       Fast/Slow: 7B VL proposes, 32B VL verifies/plans; no-AX frames skip Fast
  ├─ Action/Healer/Planner          post-action frame assertion (pixel diff), multi-locator self-heal, FSM + State Guard
  ├─ Masker (mask.py)               sensitive-region masking, FAIL-CLOSED (no/unusable AX tree → full-frame blur, never raw pixels)
  ├─ Recorder → Translator → Replayer   capture GUI events → generalize fixed coords to semantic script → replay with per-step assertion
  └─ Ops: metrics.py / audit_log.py / circuit_breaker.py / replay_ledger.py / coordination.py / prometheus.py
```

Adapters (`os_agent/adapters/`) — one per sibling, defined by the `Adapter` protocol in `base.py`:

- `executor.py` → fusion-executor (`gui_action`, 18 GuiAction kinds; runs via `asyncio.to_thread`)
- `mlx.py` → fusion-mlx multimodal chat at `localhost:11434` (guarded by circuit breaker + `asyncio.wait_for`)
- `browser.py` → fusion-browser UDS JSON-RPC (one socket, send/recv serialized under a lock, 16 MiB response cap)
- `agent_studio.py` → fusion-agent-studio HTTP

Loops (`os_agent/loops/`) compose osagent with siblings: `code_debug.py` (fusion-code visual-debug loop), `autotest.py` (fusion-autotest acceptance loop).

Supporting modules: `ax_tree.py` (the ONE unified AX-tree parser — don't write another recursive walker), `som.py` (Set-of-Mark annotation), `trajectory.py` (Bezier human-like mouse paths), `crop_zoom.py` (fine grounding), `image_cache.py` + `vlm_cache.py` (shared decode cache / VLM result cache — all caches use full sha1 hexdigest keys).

## Key invariants (from the audit hardening passes — don't regress these)

- **Fail-closed masking**: gate on the parsed tree (`tree is None or not tree.children`), never on string truthiness. Raw pixels must never reach a VLM.
- **Blocking work off the event loop**: PIL ops, file locks, executor calls, translator describer → `asyncio.to_thread`.
- **Idempotent replay**: mutating steps go through `ReplayLedger.claim(seq)` (atomic pre-execution claim), not check-then-mark — `replay` and `replay_recording` both.
- **Bounded retries/heals**: planner `max_heal_cycles` caps total successful heals; per-step retry budget resets on success.
- **Coordination files** (`nodes.json`, ledger) are flock-guarded; `_FileLock` is deliberately non-reentrant.
- Coordinate space at the API is logical points; adapters convert via `scale_factor` (default 2.0; executor capability query is upstream issue E1).

## Configuration

All knobs are env-driven `OSA_*` vars consumed by `OsaConfig` (`os_agent/config.py`) — stub mode, model names, timeouts, breaker (`OSA_BREAKER_*`), audit path/rotation (`OSA_AUDIT_*`), cluster dir/agent id (`OSA_CLUSTER_DIR`, `OSA_AGENT_ID`), trajectory seed. Add new config there; don't read env vars ad hoc elsewhere. Stub mode skips cluster wiring entirely.

## Conventions

- 4-space multiples indentation, no docstrings, logging everywhere (fusion-core `get_logger`).
- LLM/VLM HTTP goes through fusion-core clients (`get_async_client`, `FusionMLXClient`); JSON parsing uses the shared `_extract_json`-style fenced-block preference (chatty model output must not break it).
- README.md documents each hardening/feature pass — update it (and README_CN.md) when adding a feature.
- Cross-repo issues: see AGENTS.md — allowed to file against any `dahai80/fusion-*` repo, must document source repo, dependency contract, and blocking relationship, and comment back on the parent issue.
- Ports/process titles: osagent binds no business port, but `serve-metrics` defaults to `127.0.0.1:9100`; any new port must be registered in `architecture/port-registry.yaml` and long-running processes must set a `fusion-osagent ...` proctitle.
