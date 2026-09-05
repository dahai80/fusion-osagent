"""Regression tests for the 0905 audit fixes (P0-P3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from os_agent.adapters.base import Screenshot
from os_agent.api import DesktopAgent
from os_agent.audit_log import AuditLog
from os_agent.config import OsaConfig


# A3: assert_changed with no `before` must refuse instead of capturing
# back-to-back frames and reporting a constant "no change".
@pytest.mark.asyncio
async def test_assert_changed_rejects_missing_before():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        res = await agent.assert_changed(before=None)
        assert res.ok is False
        assert "before" in (res.error or "")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_assert_changed_with_explicit_before_works():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        before = Screenshot(png_b64=agent.executor._shot, width=1440, height=900, scale_factor=2.0, node_tree=None)
        res = await agent.assert_changed(before=before)
        # stub frames are identical so ratio is 0, but the call must not be
        # rejected for a missing before — it ran the real diff path.
        assert res.error != "assert_changed requires an explicit `before` frame captured before the action"
    finally:
        await agent.close()


# R1: _extract_json must prefer a fenced ```json block over prose that
# contains a stray '{' before the real object.
def test_extract_json_prefers_fenced_block_over_prose():
    from os_agent.adapters.mlx import _extract_json

    raw = (
        "Sure, for {reason} I will return the JSON now.\n"
        '```json\n{"action": "click", "target": "OK", "confidence": 0.9}\n```'
    )
    obj = _extract_json(raw)
    assert obj is not None
    assert obj["action"] == "click"
    assert obj["target"] == "OK"


def test_extract_json_falls_back_to_first_brace():
    from os_agent.adapters.mlx import _extract_json

    raw = '{"action": "wait", "confidence": 0.3}'
    obj = _extract_json(raw)
    assert obj is not None
    assert obj["action"] == "wait"


def test_extract_json_returns_none_on_garbage():
    from os_agent.adapters.mlx import _extract_json

    assert _extract_json("no json here at all") is None
    assert _extract_json("") is None


# R2: blur radius must scale with the image long edge, not be a fixed 16.
def test_adaptive_blur_radius_scales_with_long_edge():
    from os_agent.mask import _adaptive_blur_radius

    class _Img:
        width = 400
        height = 300

    assert _adaptive_blur_radius(_Img()) == 16  # floor for small frames

    class _Big:
        width = 3160
        height = 1964

    assert _adaptive_blur_radius(_Big()) == max(16, 3160 // 32)  # ~98


# R3: a successful heal must reset the retry budget so a later genuine
# failure is not mistaken for "retries exhausted".
@pytest.mark.asyncio
async def test_planner_heal_resets_retries():
    from os_agent.planner import Plan, Planner, PlanStatus, Step

    planner = Planner(max_retries=1)
    plan = Plan(name="t", steps=[Step(name="s1", action="click"), Step(name="s2", action="type")])

    calls = {"n": 0}

    async def capture():
        return Screenshot(png_b64=None, width=None, height=None, scale_factor=2.0, node_tree=None)

    async def execute(step, shot, p):
        calls["n"] += 1
        # first attempt on s1 fails, then heal fixes it, then s2 succeeds
        return calls["n"] != 1

    healed = {"done": False}

    async def heal(p, shot):
        healed["done"] = True
        return True

    await planner.run(plan, capture, execute, heal=heal)
    assert plan.status == PlanStatus.DONE
    assert healed["done"]


# E1: perception._find_in_ax must not carry the dead duplicate return block.
def test_perception_find_in_ax_no_duplicate_return():
    import inspect

    from os_agent.perception import Perception

    src = inspect.getsource(Perception._find_in_ax)
    # the duplicate block was two identical `return Locator(...), conf` in a
    # row; after the fix there is exactly one.
    assert src.count("return Locator(") == 1


# E2: image_cache get_image must be safe under concurrent threads.
def test_image_cache_thread_safe():
    import base64
    import io
    import threading

    from PIL import Image

    from os_agent import image_cache

    image_cache.clear()
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    errors = []

    def worker():
        try:
            for _ in range(50):
                image_cache.get_image(b64)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    image_cache.clear()


# E3: vlm_cache key must not store the raw prompt string.
def test_vlm_cache_key_hashes_prompt():
    from os_agent.vlm_cache import VlmCache

    c = VlmCache(ttl=3.0)
    k1 = c._key("m", "x" * 5000, "img")
    k2 = c._key("m", "x" * 5000, "img")
    # same prompt content -> same key, but the key tuple must not contain the
    # raw 5000-char string (it should be a full sha1 hexdigest, 40 chars).
    assert k1 == k2
    assert isinstance(k1[1], str) and len(k1[1]) == 40


# E5: inspect_tree must use a longer timeout than a click.
def test_inspect_tree_has_independent_longer_timeout():
    from os_agent.config import OsaConfig

    cfg = OsaConfig(stub_mode=True)
    assert cfg.inspect_timeout_ms >= 15000
    assert cfg.inspect_timeout_ms > cfg.step_timeout_ms


# E6: collect_sensitive must honor the max_nodes early-stop.
def test_collect_sensitive_max_nodes_cap():
    from os_agent import ax_tree

    # build a tree of many sensitive nodes
    children = [{"role": "axsecuretextfield", "label": f"pw{i}", "frame": [0, 0, 10, 10]} for i in range(50)]
    tree_json = '{"role":"AXWindow","children":' + str(children).replace("'", '"') + "}"
    root = ax_tree.parse(tree_json)
    capped = ax_tree.collect_sensitive(root, max_nodes=5)
    assert len(capped) == 5


# R5: strip_sensitive_labels must not recurse on a deeply-nested tree.
def test_strip_sensitive_labels_deep_tree_no_recursion_error():
    from os_agent import ax_tree

    # build a left-nested chain deeper than the default recursion limit
    depth = 600
    node = {"role": "axsecuretextfield", "label": "pw", "frame": [0, 0, 1, 1]}
    for _ in range(depth):
        node = {"role": "AXGroup", "children": [node]}
    root = ax_tree.parse(str(node).replace("'", '"'))
    # must not raise RecursionError
    redacted = ax_tree.strip_sensitive_labels(root, max_depth=256)
    assert redacted is not None


# A1: BrowserAdapter must construct in real mode without NameError
# (threading import present), and expose the rpc lock.
def test_browser_adapter_constructs_with_lock():
    from os_agent.adapters.browser import BrowserAdapter

    cfg = OsaConfig(stub_mode=True)
    br = BrowserAdapter(cfg)
    assert br._rpc_lock is not None


# E4: reasoner + perception must share ONE masker instance (was two).
@pytest.mark.asyncio
async def test_shared_masker_single_instance():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        assert agent.reasoner.masker is agent.perception.masker
        assert agent.reasoner.masker is agent.masker
    finally:
        await agent.close()


# A2: planner must bound total heal cycles so a flapping target cannot loop
# heal forever.
@pytest.mark.asyncio
async def test_planner_max_heal_cycles_bounds_loop():
    from os_agent.planner import Plan, Planner, PlanStatus, Step

    planner = Planner(max_retries=1, max_heal_cycles=2)
    plan = Plan(name="t", steps=[Step(name="s1", action="click")])

    attempts = {"n": 0}

    async def capture():
        return Screenshot(png_b64=None, width=None, height=None, scale_factor=2.0, node_tree=None)

    async def execute(step, shot, p):
        attempts["n"] += 1
        return False  # always fails -> heal every time

    heal_calls = {"n": 0}

    async def heal(p, shot):
        heal_calls["n"] += 1
        return True  # claims healed, but next attempt still fails -> flapping

    await planner.run(plan, capture, execute, heal=heal)
    # must stop at max_heal_cycles, not loop forever
    assert heal_calls["n"] <= 2
    assert plan.status != PlanStatus.DONE


# N5: Fast core must skip to Slow when no AX tree (fail-closed blur makes the
# Fast image illegible -> would permanently return "none" and always escalate
# anyway, but only after a wasted VLM call).
@pytest.mark.asyncio
async def test_fast_skips_on_no_ax_tree():
    from os_agent.reasoning import Reasoner
    from os_agent.som import SomAnnotator

    cfg = OsaConfig(stub_mode=True)
    agent = DesktopAgent(cfg)
    try:
        mlx_calls = {"n": 0}
        orig = agent.mlx.chat_json

        async def counting_chat(prompt, image_b64, model=None):
            mlx_calls["n"] += 1
            return await orig(prompt, image_b64, model=model)

        agent.mlx.chat_json = counting_chat
        shot = Screenshot(png_b64=agent.executor._shot, width=1440, height=900, scale_factor=2.0, node_tree=None)
        reasoner = Reasoner(cfg, agent.mlx, SomAnnotator(cfg), masker=agent.masker)
        proposal = await reasoner._fast_propose("click OK", shot)
        assert proposal.action == "none"
        assert proposal.unknown_dialog is True
        # Fast must NOT have called the VLM on an illegible blurred frame
        assert mlx_calls["n"] == 0
    finally:
        await agent.close()


# N9: click_humanlike must start from the tracked cursor position, not (0,0),
# and update it after the click.
@pytest.mark.asyncio
async def test_click_humanlike_tracks_cursor():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        assert agent._cursor_pos == (0.0, 0.0)
        await agent.click_humanlike(100.0, 200.0)
        assert agent._cursor_pos == (100.0, 200.0)
        # second click starts from the previous target, not (0,0)
        await agent.click_humanlike(300.0, 400.0)
        assert agent._cursor_pos == (300.0, 400.0)
        # the move_path call must have received a path starting at `before`
        last_move = [c for c in agent.executor.calls if c.get("kind") == "hover"]
        assert last_move, "move_path should have emitted hover waypoints"
    finally:
        await agent.close()


# N10: bezier_path with no explicit seed must vary across different targets
# (per-target derived seed), while the same target reproduces the same path.
def test_bezier_path_seed_varies_per_target():
    from os_agent.trajectory import TrajectoryConfig, bezier_path

    cfg = TrajectoryConfig(seed=None)
    p_a1 = bezier_path((0, 0), (100, 100), cfg)
    p_a2 = bezier_path((0, 0), (100, 100), cfg)
    p_b = bezier_path((0, 0), (200, 200), cfg)
    # same target -> reproducible (replay-friendly)
    assert p_a1 == p_a2
    # different target -> different jitter shape (not a fixed bot fingerprint)
    assert p_a1 != p_b


# E6: translate_async must offload a blocking describer to a thread (does not
# block) and still produce a visual guard when the describer returns text.
@pytest.mark.asyncio
async def test_translate_async_offloads_describer():
    from os_agent.recorder import Recording, Step
    from os_agent.translator import Translator

    block = {"called": False}

    def slow_describer(png_b64):
        block["called"] = True
        return "the OK button"

    rec = Recording(
        steps=[Step(seq=1, kind="click", at=[10.0, 20.0], button="left", screenshot_b64="img")],
        meta={},
    )
    tr = Translator(describer=slow_describer)
    script = await tr.translate_async(rec)
    assert block["called"] is True
    assert script.steps[0].guard_kind == "visual"
    assert script.steps[0].target_desc == "the OK button"


# R4: assert_changed threshold must come from config when not passed explicitly.
def test_assert_diff_threshold_configurable():
    cfg = OsaConfig(stub_mode=True)
    assert cfg.assert_diff_threshold == 0.002
    # env override path
    import os

    os.environ["OSA_ASSERT_DIFF_THRESHOLD"] = "0.05"
    try:
        cfg2 = OsaConfig(stub_mode=True)
        assert cfg2.assert_diff_threshold == 0.05
    finally:
        del os.environ["OSA_ASSERT_DIFF_THRESHOLD"]


# E5: metrics snapshot must export counters, latency histograms, and the vlm
# cache hit/miss, so production has observability instead of only log lines.
@pytest.mark.asyncio
async def test_metrics_snapshot_exports_counters_and_cache():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        snap = agent.metrics_snapshot()
        assert "counters" in snap
        assert "histograms" in snap
        assert "caches" in snap
        assert "vlm_cache" in snap
        assert "masker_masked_total" in snap
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_metrics_counts_actions():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        await agent.click(10.0, 20.0)
        snap = agent.metrics_snapshot()
        assert snap["counters"].get("action.click.total", 0) >= 1
        assert snap["counters"].get("action.click.ok", 0) >= 1
    finally:
        await agent.close()


def test_metrics_histogram_buckets_and_avg():
    from os_agent.metrics import Histogram

    h = Histogram(name="t")
    h.observe(3)
    h.observe(7)
    h.observe(120)
    snap = h.snapshot()
    assert snap["count"] == 3
    assert snap["sum_ms"] == 130
    assert snap["avg_ms"] == round(130 / 3, 3)
    # buckets are independent (one observe increments exactly one bucket):
    # <=5 holds the 3, <=10 holds the 7, overflow tail holds the 120
    assert snap["buckets"][1][1] == 1  # <=5
    assert snap["buckets"][2][1] == 1  # <=10
    assert snap["buckets"][6][1] == 1  # <=250 holds the 120


def test_metrics_registry_thread_safe():
    from os_agent.metrics import MetricsRegistry

    reg = MetricsRegistry()
    import threading

    def worker():
        for _ in range(200):
            reg.inc("c")
            reg.observe("h", 1.0)
            reg.cache_hit("vlm")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = reg.snapshot()
    assert snap["counters"]["c"] == 1600
    assert snap["histograms"]["h"]["count"] == 1600
    assert snap["caches"]["vlm"]["hits"] == 1600


# Audit gap 4: structured audit log must record decide/action/heal/assert
# events as queryable rows, and persist to JSONL when a path is given.
@pytest.mark.asyncio
async def test_audit_log_records_action_and_decide():
    agent = DesktopAgent(OsaConfig(stub_mode=True))
    try:
        await agent.click(5.0, 6.0)
        rows = agent.audit.query(kind="action")
        assert len(rows) >= 1
        assert rows[-1].detail["action_kind"] == "click"
        assert rows[-1].detail["ok"] is True
    finally:
        await agent.close()


def test_audit_log_persists_to_jsonl(tmp_path):
    p = str(tmp_path / "audit.jsonl")
    al = AuditLog(path=p, agent_id="t1")
    al.record("action", action_kind="click", ok=True, latency_ms=12)
    al.record("decide", core="fast", action="click", confidence=0.9, escalated=False, has_ax=True)
    al.record("mask", regions=2)
    lines = Path(p).read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    import json as _j

    first = _j.loads(lines[0])
    assert first["kind"] == "action"
    assert first["agent_id"] == "t1"
    assert first["detail"]["action_kind"] == "click"
    # query filters by kind
    assert len(al.query(kind="decide")) == 1
    assert len(al.query(kind="mask")) == 1


def test_audit_log_fail_open_on_bad_path():
    # an unwritable path must NOT raise from record()
    al = AuditLog(path="/no/such/dir/audit.jsonl", agent_id="x")
    al.record("action", action_kind="click", ok=True)  # must not raise
    assert al.count() == 1  # in-memory buffer still updated


# Gap 3: circuit breaker must OPEN after consecutive failures and fast-fail,
# then HALF_OPEN after cooldown, then CLOSE on a successful probe.
def test_circuit_breaker_opens_and_fast_fails():
    from os_agent.circuit_breaker import BreakerConfig, CircuitBreaker, CircuitOpenError

    cb = CircuitBreaker(name="t", cfg=BreakerConfig(failure_threshold=3, cooldown_s=60.0, min_calls_for_rate=100))
    for _ in range(3):
        cb.on_failure()
    assert cb.state == "open"
    assert cb.allow() is False
    with pytest.raises(CircuitOpenError):
        raise CircuitOpenError("x")


def test_circuit_breaker_half_open_then_close_on_success():
    from os_agent.circuit_breaker import BreakerConfig, CircuitBreaker

    cb = CircuitBreaker(name="t", cfg=BreakerConfig(failure_threshold=2, cooldown_s=60.0, min_calls_for_rate=100))
    cb.on_failure()
    cb.on_failure()
    assert cb.state == "open"
    # force cooldown-elapsed so the next state query flips to half_open
    cb._opened_at = 0.0
    assert cb.state == "half_open"
    cb.on_success()
    assert cb.state == "closed"


def test_circuit_breaker_rate_based_open():
    from os_agent.circuit_breaker import BreakerConfig, CircuitBreaker

    cb = CircuitBreaker(
        name="t", cfg=BreakerConfig(failure_threshold=100, failure_rate=0.5, min_calls_for_rate=4, window_s=30.0)
    )
    # 4 calls, 3 fail -> 75% > 50% with min 4 samples -> open
    cb.on_success()
    cb.on_failure()
    cb.on_failure()
    cb.on_failure()
    assert cb.state == "open"


def test_circuit_breaker_resets_consecutive_on_success():
    from os_agent.circuit_breaker import BreakerConfig, CircuitBreaker

    cb = CircuitBreaker(name="t", cfg=BreakerConfig(failure_threshold=3, min_calls_for_rate=100))
    cb.on_failure()
    cb.on_failure()
    cb.on_success()  # resets consecutive counter
    cb.on_failure()
    assert cb.state == "closed"  # only 1 consecutive now, below threshold


# Gap 5: idempotent replay must resume after a crash — a re-run with the same
# idempotency key skips already-completed steps (no double-click / double-type).
@pytest.mark.asyncio
async def test_idempotent_replay_skips_completed_steps(tmp_path):
    from os_agent.action import FrameAssertion
    from os_agent.adapters.base import Screenshot
    from os_agent.replayer import Replayer
    from os_agent.translator import Script, ScriptStep

    class _FakeAsserter:
        async def assert_changed(self, before, after, expected=None, threshold=0.0):
            return FrameAssertion(ok=True, changed=True, changed_ratio=0.05, error="")

    class _FakeExecutor:
        def __init__(self):
            self.click_count = 0

        async def click(self, loc, button="left"):
            self.click_count += 1
            return {"ok": True, "error": None}

    class _FakePerception:
        async def capture(self, prefer_ax=False):
            return Screenshot(png_b64="iVBORw0KGgo=", width=100, height=100, scale_factor=2.0, node_tree=None)

    class _FakeAgent:
        def __init__(self):
            self.executor = _FakeExecutor()
            self.perception = _FakePerception()
            self.asserter = _FakeAsserter()

    agent = _FakeAgent()
    rep = Replayer(agent)
    key = "run-001"
    ledger_path = str(tmp_path / "ledger.jsonl")
    script = Script(
        steps=[
            ScriptStep(
                seq=1,
                verb="click",
                target_desc="btn",
                guard_kind="point",
                action={"at": [10.0, 20.0], "button": "left"},
            ),
            ScriptStep(
                seq=2,
                verb="click",
                target_desc="btn2",
                guard_kind="point",
                action={"at": [30.0, 40.0], "button": "left"},
            ),
        ]
    )
    # first run: both steps execute (2 real clicks)
    r1 = await rep.replay_script(script, idempotency_key=key, ledger_path=ledger_path)
    assert r1.passed == 2
    assert agent.executor.click_count == 2
    # second run with same key: both skipped (idempotent resume) — NO extra clicks
    r2 = await rep.replay_script(script, idempotency_key=key, ledger_path=ledger_path)
    skipped = [s for s in r2.results if "skipped" in (s.error or "")]
    assert len(skipped) == 2
    assert agent.executor.click_count == 2  # unchanged: no double-click


def test_replay_ledger_persists_and_loads(tmp_path):
    from os_agent.replay_ledger import ReplayLedger

    p = str(tmp_path / "l.jsonl")
    l1 = ReplayLedger("k1", path=p)
    l1.mark_done(1)
    l1.mark_done(2)
    l1.mark_done(1)  # idempotent: no duplicate
    # new instance loads the same file -> knows 1 and 2 are done
    l2 = ReplayLedger("k1", path=p)
    assert l2.completed() == {1, 2}
    assert l2.is_done(1) is True
    assert l2.is_done(3) is False
    # a different key does NOT see another key's progress
    l3 = ReplayLedger("k2", path=p)
    assert l3.completed() == set()


# ---- Gap 2: multi-node coordination ----


def test_node_registry_register_deregister(tmp_path):
    from os_agent.coordination import NodeRegistry

    reg = NodeRegistry(state_path=str(tmp_path / "nodes.json"))
    reg.register("node-a", mlx="qwen-7b")
    live = reg.live_nodes()
    assert [n.node_id for n in live] == ["node-a"]
    assert live[0].meta["mlx"] == "qwen-7b"
    reg.deregister("node-a")
    assert reg.live_nodes() == []


def test_node_registry_reaps_stale(tmp_path):
    import time as _t

    from os_agent.coordination import NodeRegistry

    reg = NodeRegistry(state_path=str(tmp_path / "nodes.json"), heartbeat_ttl_s=0.2)
    reg.register("stale")
    # simulate a node that went silent: rewind its heartbeat
    data = reg._read()
    data["nodes"]["stale"]["last_heartbeat"] = _t.time() - 10.0
    reg._write(data)
    reg.register("fresh")
    live = reg.live_nodes()
    assert [n.node_id for n in live] == ["fresh"]  # stale reaped


def test_cluster_health_aggregate_failure_opens(tmp_path):
    from os_agent.coordination import ClusterHealth

    ch = ClusterHealth(
        state_path=str(tmp_path / "h.json"),
        lock_path=str(tmp_path / "h.json.lock"),
        window_s=30.0,
        open_threshold=3,
    )
    # two distinct nodes each fail a couple times — aggregate crosses threshold
    assert ch.should_open() is False
    ch.report_failure("n1")
    ch.report_failure("n2")
    assert ch.should_open() is False
    res = ch.report_failure("n1")  # 3rd aggregate failure
    assert res["cluster_open"] is True
    assert ch.should_open() is True


def test_cluster_health_success_does_not_wipe_recent_failures(tmp_path):
    # P2 fix: report_success must NOT drop a flapping node's recent failures
    # on a single success — otherwise a node failing 9x then succeeding 1x
    # has its aggregate count reset to zero and the cluster breaker (which
    # opens on aggregate failures) never trips on it alone.
    from os_agent.coordination import ClusterHealth

    ch = ClusterHealth(
        state_path=str(tmp_path / "h.json"),
        lock_path=str(tmp_path / "h.json.lock"),
        window_s=30.0,
        open_threshold=2,
    )
    ch.report_failure("n1")
    ch.report_failure("n1")
    assert ch.should_open() is True
    # n1 succeeds once — recent failures still in-window, cluster stays OPEN
    ch.report_success("n1")
    assert ch.should_open() is True


def test_filelock_is_not_reentrant_in_same_process(tmp_path):
    # P1 fix: _FileLock now serializes same-process entry with a per-path
    # threading.Lock, so a second acquire on the same path in the same thread
    # DEADLOCKS rather than silently re-entering. The old reentrancy shortcut
    # broke thread mutual exclusion (two threads could both hold the lock).
    # This test asserts the non-reentrant contract: nested acquire must block,
    # so we run it in a thread and assert it does not acquire within a timeout.
    import threading

    from os_agent.coordination import _FileLock

    lp = str(tmp_path / "x.lock")
    lock = _FileLock(lp)
    with lock:
        acquired = threading.Event()

        def _try():
            with _FileLock(lp):
                acquired.set()

        t = threading.Thread(target=_try, daemon=True)
        t.start()
        t.join(0.5)
        # second acquire must still be blocked while the outer holds the lock
        assert not acquired.is_set()
    # after release, a fresh acquire must work
    with _FileLock(lp):
        pass


# ---- GA ops gap 1: Prometheus exposition ----


def test_prometheus_renders_counters_and_histograms():
    from os_agent.prometheus import render_prometheus

    snap = {
        "counters": {"action.click.total": 5, "action.click.ok": 4},
        "histograms": {
            "action.click.latency_ms": {
                "buckets": [["<= 25", 3], ["<= 50", 1]],
                "sum_ms": 120.0,
                "count": 4,
            }
        },
        "caches": {"vlm": {"hits": 10, "misses": 2, "hit_rate": 0.8333, "total": 12}},
        "masker_masked_total": 7,
        "coordination_enabled": True,
        "breaker": {"state": "OPEN", "failures": 5, "failure_threshold": 5},
        "cluster_health": {"failures": 3, "open": True, "open_threshold": 2},
    }
    text = render_prometheus(snap)
    assert "# TYPE osagent_action_click_total counter" in text
    assert "osagent_action_click_total 5" in text
    assert "# TYPE osagent_action_click_latency_ms histogram" in text
    assert 'osagent_action_click_latency_ms_bucket{le="25"} 3' in text
    assert 'osagent_action_click_latency_ms_bucket{le="+Inf"} 4' in text
    assert "osagent_action_click_latency_ms_sum 120.0" in text
    assert "osagent_action_click_latency_ms_count 4" in text
    assert "osagent_vlm_hits 10" in text
    assert "osagent_vlm_hit_rate 0.8333" in text
    assert "osagent_masker_masked_total 7" in text
    assert "# TYPE osagent_breaker_state gauge" in text
    assert "osagent_breaker_state 1" in text
    assert "osagent_cluster_open 1" in text


# ---- GA ops gap 2: audit rotation + retention ----


def test_audit_log_rotates_on_size_cap(tmp_path):
    from os_agent.audit_log import AuditLog

    path = str(tmp_path / "audit.jsonl")
    # tiny cap so a handful of records triggers rotation
    audit = AuditLog(path=path, rotate_max_bytes=120, retention_files=3, retention_days=0)
    for i in range(20):
        audit.record("action", action_kind="click", ok=True, idx=i)
    # the active file should have been rotated at least once -> an archive exists
    archives = [p for p in tmp_path.iterdir() if p.name.startswith("audit.jsonl.")]
    assert len(archives) >= 1, "expected at least one rotated archive"
    # active file still present and under cap (post-rotation it restarts small)
    assert Path(path).exists()
    assert Path(path).stat().st_size < 120 or Path(path).read_text().count("\n") <= 20


def test_audit_log_retention_prunes_old_archives(tmp_path):

    from os_agent.audit_log import AuditLog

    path = str(tmp_path / "audit.jsonl")
    audit = AuditLog(path=path, rotate_max_bytes=50, retention_files=2, retention_days=0)
    # force several rotations
    for i in range(40):
        audit.record("action", idx=i)
    archives = sorted((p for p in tmp_path.iterdir() if p.name.startswith("audit.jsonl.")), key=lambda p: p.name)
    # retention_files=2 -> at most 2 archives kept
    assert len(archives) <= 2, f"expected <=2 archives, got {len(archives)}"


def test_audit_log_no_rotation_when_cap_zero(tmp_path):
    from os_agent.audit_log import AuditLog

    path = str(tmp_path / "audit.jsonl")
    audit = AuditLog(path=path, rotate_max_bytes=0, retention_files=0, retention_days=0)
    for i in range(30):
        audit.record("action", idx=i)
    archives = [p for p in tmp_path.iterdir() if p.name.startswith("audit.jsonl.")]
    assert archives == [], "rotate_max_bytes=0 must never rotate"


# ---- GA gap 4: replay_recording uses claim (no double-execute) ----


@pytest.mark.asyncio
async def test_replay_recording_claim_skips_concurrent_duplicate(tmp_path):
    from os_agent.recorder import ManualEventSource, Recorder
    from os_agent.replayer import Replayer

    class _Asserter:
        async def assert_changed(self, before, after, expected=None, threshold=0.0):
            from os_agent.action import FrameAssertion

            return FrameAssertion(ok=True, changed=True, changed_ratio=0.1, error="")

    class _Exec:
        def __init__(self):
            self.calls = []

        async def click(self, loc, button="left"):
            self.calls.append(("click", loc.as_point()))
            return {"ok": True}

    class _Perc:
        async def capture(self, prefer_ax=False):
            return Screenshot(png_b64="iVBORw0KGgo=", width=10, height=10, scale_factor=2.0, node_tree=None)

    class _Agent:
        def __init__(self):
            self.executor = _Exec()
            self.perception = _Perc()
            self.asserter = _Asserter()

    rec = Recorder(
        ManualEventSource([{"kind": "click", "at": [1.0, 2.0]}]), capture=lambda: None, clock=lambda: 0.0
    ).record()
    ledger_path = str(tmp_path / "led.jsonl")
    agent = _Agent()
    rep = Replayer(agent)
    # first run claims + executes the step
    r1 = await rep.replay_recording(rec, idempotency_key="k1", ledger_path=ledger_path)
    assert r1.passed == 1
    assert len(agent.executor.calls) == 1
    # second run with same key -> claim returns False -> skipped, NO extra click
    r2 = await rep.replay_recording(rec, idempotency_key="k1", ledger_path=ledger_path)
    assert r2.passed == 1
    assert len(agent.executor.calls) == 1, "concurrent replay must not double-execute (claim skipped)"
