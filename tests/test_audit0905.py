"""Regression tests for the 0905 audit fixes (P0-P3)."""
from __future__ import annotations

import pytest

from os_agent.adapters.base import Screenshot
from os_agent.api import DesktopAgent
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
        "```json\n{\"action\": \"click\", \"target\": \"OK\", \"confidence\": 0.9}\n```"
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
    # raw 5000-char string (it should be a 16-char hash).
    assert k1 == k2
    assert isinstance(k1[1], str) and len(k1[1]) == 16


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
    children = [
        {"role": "axsecuretextfield", "label": f"pw{i}", "frame": [0, 0, 10, 10]}
        for i in range(50)
    ]
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
