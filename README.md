# fusion-osagent

> Desktop Embodied AI — pixel-level OS Agent barrier layer for Apple Silicon.

`fusion-osagent` is the desktop-embodied counterpart to `fusion-robot`: where
`fusion-robot` drives voxel-level physical robotics, `fusion-osagent` drives
pixel-level desktop GUI automation. It reuses sibling fusion packages as
primitives rather than rebuilding them, adding the five barrier layers that
turn raw GUI access into a reliable agent:

1. **AX + visual dual-track perception** — AXUI tree first, visual grounding fallback.
2. **Set-of-Mark (SOM) annotation** — overlay numbered marks on screenshot + AX tree.
3. **Fast / Slow dual-core reasoning** — small VL model proposes, large VL model verifies/plans.
4. **Post-action frame assertion + self-healing** — assert state changed, re-locate on miss.
5. **Human-like trajectories** — Bezier mouse paths, sensitive-region masking, record/replay.

The API aligns with the Claude Computer Use `computer` tool
(`screenshot` / `click` / `type` / `key` / `scroll` / `drag` / `wait`) and
extends it with `assert` / `heal` / `som_view` / `replay`.

## Architecture

```
                   DesktopAgent  (os_agent.api)
                        |
              Perception (os_agent.perception)
              /         |          \
   ExecutorAdapter  MlxAdapter  BrowserAdapter   AgentStudioAdapter
        |              |              |                 |
  fusion-executor  fusion-mlx   fusion-browser    fusion-agent-studio
  (AXUI/CGEvent)   (inference)  (Web AXTree/CDP)  (orchestration)
        |
  fusion-core (HTTP/LLM client, config, logging)
```

- `os_agent/config.py` — env-driven `OsaConfig`, point↔pixel conversion.
- `os_agent/ax_tree.py` — unified AX-tree parser (parse / find_by_label / find_by_role / collect_interactive / collect_sensitive / guess_role / strip_sensitive_labels).
- `os_agent/image_cache.py` — shared decoded-image LRU cache (mask / SOM / diff reuse one decode per frame).
- `os_agent/vlm_cache.py` — VLM result cache (TTL+LRU, keyed on model+prompt+image hash; skips re-inference on identical input).
- `os_agent/adapters/base.py` — `Locator`, `Screenshot`, `Adapter` protocol.
- `os_agent/adapters/executor.py` — wraps `fusion-executor` `gui_action` (18 GuiAction kinds).
- `os_agent/adapters/mlx.py` — wraps `fusion-mlx` multimodal chat.
- `os_agent/adapters/browser.py` — UDS JSON-RPC to `fusion-browser`.
- `os_agent/adapters/agent_studio.py` — HTTP to `fusion-agent-studio`.
- `os_agent/perception.py` — dual-track locate (AX search → visual grounding).
- `os_agent/api.py` — `DesktopAgent`, the single entry point.
- `os_agent/som.py` — Set-of-Mark annotation (AX-boundary, numbered marks).
- `os_agent/action.py` — post-action frame assertion (pixel diff + VLM verify).
- `os_agent/healer.py` — multi-locator self-healing (ax-label → ax-role → visual).
- `os_agent/planner.py` — FSM planner + State Guard.
- `os_agent/mask.py` — sensitive-region masking before VLM.
- `os_agent/reasoning.py` — Fast/Slow dual-core scheduler (Phase 2.1).
- `os_agent/trajectory.py` — human-like Bezier mouse path + key jitter (Phase 2.2).
- `os_agent/crop_zoom.py` — patch-level crop & zoom for fine grounding (Phase 2.3).
- `os_agent/loops/code_debug.py` — fusion-code visual-debug loop: patch → verify → feedback (F5.2).
- `os_agent/loops/autotest.py` — fusion-autotest acceptance loop: osagent executes, autotest asserts (F5.3).
- `os_agent/recorder.py` — GUI trajectory capture: events + screenshot context → structured Steps (F4.1).
- `os_agent/translator.py` — fixed-coord Steps → natural-language script with Semantic Guards (F4.2).
- `os_agent/replayer.py` — step-by-step replay through DesktopAgent with per-step frame assertion (F4.3).
- `os_agent/cli.py` — `fusion-osagent` CLI (preflight / screenshot / click / health).

## Sibling reuse (no rebuild)

| Sibling | Role | Contract |
|---------|------|----------|
| `fusion-core` | HTTP/LLM client, config, logging | `get_async_client`, `get_logger` |
| `fusion-executor` | GUI actuation | `FusionSandboxExecutor.gui_action(dict) -> GuiResult`, 18 kinds |
| `fusion-mlx` | VL inference | `FusionMLXClient` at `localhost:11434` |
| `fusion-browser` | Web page AX tree + CDP | UDS JSON-RPC (no Python client yet) |
| `fusion-agent-studio` | orchestration | HTTP API, 37 built-in tools / 11 NodeType |
| `fusion-autotest` | assertions | post-action frame assertion (Phase 1) |
| `fusion-code` | coding loop | visual debug for software-engineering tasks (Phase 2) |

Gaps in sibling APIs are filed as upstream issues, not patched here
(see `architecture/fusion-osagent-prd-0904.md`).

## Install

```bash
cd /Users/dahai/fusion
source .venv/bin/activate
pip install -e fusion-osagent         # runtime
pip install -e "fusion-osagent[test]" # with test extras
```

## Usage

```bash
# offline software self-check (no live siblings)
fusion-osagent preflight

# capture one frame (stub mode writes a 1x1 placeholder)
fusion-osagent --stub screenshot --out frame.png
fusion-osagent screenshot --out frame.png   # real mode needs fusion-executor

# click at logical point (x, y)
fusion-osagent --stub click 100 200

# ping fusion-mlx
fusion-osagent --stub health
```

Programmatic:

```python
import asyncio
from os_agent.api import DesktopAgent
from os_agent.config import OsaConfig

async def main():
    agent = DesktopAgent(OsaConfig(stub_mode=True))   # real mode: stub_mode=False
    shot = await agent.screenshot()
    await agent.click(100.0, 200.0)
    res = await agent.click_by("OK")        # dual-track: AX first, visual fallback
    await agent.type_text("hello")
    await agent.key("Return", modifiers=["command"])
    await agent.close()

asyncio.run(main())
```

Record → translate → replay (Phase 3):

```python
import asyncio
from os_agent.api import DesktopAgent
from os_agent.config import OsaConfig
from os_agent.recorder import ManualEventSource, Recorder
from os_agent.translator import Translator

async def main():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    # record (manual source here; CGEventTapSource for real capture)
    src = ManualEventSource([
        {"kind": "click", "at": [100.0, 200.0], "button": "left"},
        {"kind": "type", "text": "hello"},
    ])
    rec = Recorder(src, capture=lambda: None, clock=lambda: 0.0).record()
    # generalize fixed coords -> semantic script
    script = agent.translator.translate(rec)
    # replay with per-step frame assertion + visual guards
    report = await agent.replay(script)
    assert report.ok
    await agent.close()

asyncio.run(main())
```

## Tests

```bash
pytest tests/ -v          # all unit/stub tests
pytest tests/test_api.py::test_click_logs_and_returns_ok -v   # single test
ruff check .              # lint
ruff format .             # format
```

- `asyncio_mode=auto`, `testpaths=["tests"]`.
- Default tests run fully offline against in-process stub adapters
  (`OSA_STUB_MODE=1` / `OsaConfig(stub_mode=True)`).
- Marker `integration` gates tests that need live siblings:
  `OSA_RUN_INTEGRATION=1 pytest -m integration` (requires fusion-mlx with a
  VL model loaded, e.g. `mlx-community/Qwen2.5-VL-7B-Instruct-4bit`).

## Coordinate space

The API exposes a **single logical-point space** (Apple "points").
Adapters convert to physical pixels via `scale_factor` (default `2.0` Retina).

```python
from os_agent.config import points_to_pixels, pixels_to_points
points_to_pixels(100.0, 200.0, 2.0)   # (200.0, 400.0)
pixels_to_points(200.0, 400.0, 2.0)   # (100.0, 200.0)
```

`scale_factor` auto-detection is pending an upstream executor capability
query (issue E1); until then it defaults to `2.0`.

## Roadmap / Phases

| Phase | Goal | Status |
|-------|------|--------|
| **0** | skeleton + stub adapters, dual-track perception, ruff/pytest green | ✅ done |
| **1** | SOM overlay, frame assertion, self-healing, FSM planner, sensitive masking, real VL E2E | ✅ done |
| **2** | Fast/Slow dual-core reasoning, Bezier trajectory, crop/zoom, code-debug + autotest loops | ✅ done |
| **3** | record/replay + generalization, human-like trajectory execution, sensitive-mask wiring | ✅ done |

Phase 0 acceptance: stub `api.screenshot()` + `api.click(x,y)` exercise the
dual-track dispatch; `ruff check .` and `pytest tests/` are green. ✅

Phase 3 acceptance: record → translate → replay loop runs with per-step frame
assertion; Semantic Guards re-locate by description instead of fixed coords;
Bezier trajectory drives human-like clicks; sensitive regions are masked
before every VLM call. ✅

## Hardening (audit 0904)

A full adversarial audit (`audit/fusion-osagent-0904.md`) drove a hardening
pass across all P0–P3 findings. Summary of what changed:

- **P0 fatal** — executor path is now real `asyncio.to_thread` (no fake-async
  blocking); browser adapter reuses one UDS socket with an atomic RPC id
  counter; sensitive masking is fail-closed (no AX tree → full-frame blur,
  never raw pixels to VLM); VLM coordinate output disambiguated (normalized
  vs pixel) and `chat_json` is fail-loud (returns `None`, never `{}`);
  Recorder `CGEventTapSource` runs the tap on a dedicated thread with a
  thread-safe queue; frame assertion counts changed pixels via the histogram
  (not bbox area); file handles closed everywhere (`with`/context managers).
- **P1 logic** — AX locate uses graded confidence (exact 0.95 → prefix 0.85
  → substring 0.7 → role 0.75) with a min query length; planner heal-retry
  is bounded and logs `exc_info`; translator guard degrades to `point` when
  no describer is present; replayer drag dispatches one real `drag` call;
  `code_debug` passes `expected=None` (no nonsense semantic match) and
  captures a real `before` frame after the click (B3).
- **P2 architecture** — a unified `os_agent/ax_tree.py` parser replaces the
  three duplicate recursive walkers (`parse`, `find_by_label` with
  exact/prefix/substring modes, `find_by_role`, `collect_interactive`,
  `collect_sensitive`, `guess_role`, `strip_sensitive_labels`); coordinates
  carry per-screenshot `scale_factor` instead of a global assumption.
- **P3 performance** — shared decoded-image cache (`os_agent/image_cache.py`)
  so mask / SOM / diff reuse one PIL decode per frame; frame diff downsamples
  to a 256px thumbnail before the histogram; a VLM result cache
  (`os_agent/vlm_cache.py`, TTL+LRU keyed on model+prompt+image hash) skips
  re-inference when `decide` is called twice with an unchanged screen.
- **Maintainability** — top-level imports (no inline `import`); key exception
  paths use `log.exception` for tracebacks; report-path writes are confined
  to an env-configurable allow-list root (`OSA_REPORT_ROOT`) with traversal
  refusal.

D14 (real end-to-end run) is environment-gated: 7 integration tests exist
behind `OSA_RUN_INTEGRATION=1` and need Accessibility TCC + a loaded VL model.
The TCC grant is a host environment step, not a code defect.

## Hardening (audit 0905)

A second adversarial audit (`audit/fusion-osagent-audit-result-0905.md`)
drove a P0–P3 hardening pass focused on concurrency, blocking, and
resolution-independent safety. Verdict of that audit: **not yet
enterprise-production-ready**; after these fixes it reaches "controlled
internal Beta". Summary of what changed:

- **P0 fatal** — `BrowserAdapter._rpc_sync` now serializes all send/recv
  under a `threading.Lock` (concurrent RPCs no longer interleave
  length-prefix frames on the shared UDS socket) and the missing
  `import threading` that crashed real-mode construction is fixed; VLM
  `chat_vision` is bounded by `asyncio.wait_for(timeout=cfg.vlm_timeout)`
  so a stuck mlx inference can no longer hang the Agent loop forever;
  `assert_changed` now refuses a missing `before` frame instead of
  capturing before+after back-to-back and reporting a constant "no change".
- **P1 high** — `_extract_json` prefers a fenced ` ```json ` block over
  prose containing a stray `{`, so chatty 7B fast output no longer
  force-escalates to slow on every turn; mask blur radius is adaptive to
  the frame long edge (`max(16, long_edge//32)`) so fail-closed blur is
  not legible on 4K Retina; planner heal success resets the retry budget;
  `CGEventTapSource` takes the real display scale instead of hardcoded
  2.0 (multi-DPI safe); `strip_sensitive_labels` / `_to_dict` are
  iterative with a depth cap so deep AX trees cannot RecursionError-crash
  the fail-closed masking path.
- **P2/P3** — perception dead duplicate return block removed; `image_cache`
  access serialized under a lock (thread-safe under `asyncio.to_thread`);
  `vlm_cache` hashes the prompt (no kilobyte strings as dict keys);
  executor `_run` actually retries once on transient timeout/exception
  (matching its docstring) and logs when `step_timeout_ms` is clamped;
  `inspect_tree` gets an independent longer timeout
  (`OSA_INSPECT_TIMEOUT_MS`, default 15s) so AX traversal is not
  misclassified as a click timeout; `collect_sensitive` early-stops.
- Regression tests for every fix live in `tests/test_audit0905.py`.

### audit 0905 — v2 supplemental fixes

A follow-up pass closed the remaining audit findings (E4, E6) plus adjacent
runtime defects (N5, N9, N10) and tightened two earlier fixes:

- **E4 (P2)** — `Reasoner` and `Perception` now share a single
  `SensitiveMasker` instance (constructed once in `DesktopAgent`). Two
  independent maskers had split `masked_count` counters and LRU caches, so
  the fast→slow escalation masked the same shot twice with no cache reuse
  and the masked-region count was dishonest.
- **E6 (P3)** — `Translator` gained `translate_async` / `_describe_async`
  that offload the (blocking, model-backed) `describer` to a worker thread
  via `asyncio.to_thread`, so translating a recording no longer pins the
  event loop for one VLM call per step with no way to cancel. The sync
  `translate()` path is unchanged for offline tests.
- **N5** — Fast core now skips to Slow directly when a frame has no AX
  tree. The fail-closed masker blurs the whole no-AX frame to an
  illegible image, so the 7B Fast core almost always returned `"none"`
  and escalated anyway — but only after a wasted VLM round-trip. Skipping
  Fast outright removes the dead inference.
- **N9** — `click_humanlike` now starts the Bezier path from the tracked
  cursor position (`DesktopAgent._cursor_pos`) instead of a fixed
  `(0,0)`. A corner-to-target teleport on every click was both visually
  jarring and a bot fingerprint; the position is updated after each click.
- **N10** — `bezier_path` with no explicit seed now derives a per-target
  seed from the start/end coords (reproducible for the same target,
  varying across targets) instead of a fixed `seed=7` that made every
  click trace the identical jitter shape — a strong bot fingerprint.
  `OSA_TRAJECTORY_SEED` now defaults to `None` (per-target); set it to a
  fixed int for strict replay.
- **A2 (tightened)** — `Planner` now bounds total successful heals with
  `max_heal_cycles` (default 4, `OSA_PLANNER_MAX_HEAL_CYCLES`) so a
  flapping step (execute fail → heal ok → execute fail …) cannot loop
  forever, even though each successful heal still resets the per-step
  retry budget.
- **R4 (tightened)** — `assert_changed` threshold is now
  `cfg.assert_diff_threshold` (default 0.002, `OSA_ASSERT_DIFF_THRESHOLD`)
  so cursor-blink false positives and small-highlight false negatives can
  be tuned per scene instead of a hardcoded constant.

Upstream blockers unchanged: E1 (executor scale_factor/capability query),
E2 (executor batch-move), B2 (browser Python client), C1 (code
visual-feedback protocol), AT1 (autotest single-requirement mode) remain
filed as issues, not patched here.

## Upstream dependencies

Hard-blocking gaps filed as issues (do not patch sibling repos here):

- **E1** — `fusion-executor` [#43](https://github.com/dahai80/fusion-executor/issues/43): expose `scale_factor` / capability query.
- **E2** — `fusion-executor` [#44](https://github.com/dahai80/fusion-executor/issues/44): batch mouse-move (waypoint path) GuiAction.
- **B2** — `fusion-browser` [#12](https://github.com/dahai80/fusion-browser/issues/12): provide a Python client for the UDS JSON-RPC API.
- **C1** — `fusion-code` [#217](https://github.com/dahai80/fusion-code/issues/217): stable visual-feedback protocol (osagent emits JSON; code consumes to auto-fix). Until then `code_debug` writes a local report + optional `--visual-feedback` CLI hook.
- **AT1** — `fusion-autotest` [#11](https://github.com/dahai80/fusion-autotest/issues/11): single-requirement VLM assertion mode (today `vlm` asserts the full PRD; osagent parses `vlm_result.json` defects).

## License

Part of the Fusion local-first Apple Silicon ecosystem. See repository root.
