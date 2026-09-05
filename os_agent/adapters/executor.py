"""fusion-executor adapter: AXUI tree + screenshot + 18 GuiAction via CGEvent.

Wraps `FusionSandboxExecutor.gui_action(action: dict) -> GuiResult` over UDS
(`~/.fusion-executor/fe.sock`, env FUSION_EXECUTOR_SOCK). Coordinate space: until
upstream issue E1 (fusion-executor#38) exposes scale_factor, we assume executor
accepts logical points and screenshots are physical pixels at 2x on Retina.

GuiAction kinds (verified v0.2.9):
  focus_app, click, type_text, key_press, hold_key, screenshot, inspect_tree,
  scroll, drag, double_click, triple_click, right_click, hover,
  window_close, window_minimize, window_zoom, window_resize, wait

The upstream client is synchronous (blocking UDS round-trip). Every call is
offloaded to a worker thread via asyncio.to_thread so the event loop is never
blocked and asyncio.wait_for / step_timeout_ms can actually interrupt a slow
gui_action (D1 fix). A per-call timeout + one retry make the adapter robust.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fusion_core import get_logger

from os_agent.adapters.base import Locator, Screenshot
from os_agent.config import OsaConfig, points_to_pixels

log = get_logger("os_agent.adapters.executor")

KIND_CLICK = "click"
KIND_DOUBLE_CLICK = "double_click"
KIND_TRIPLE_CLICK = "triple_click"
KIND_RIGHT_CLICK = "right_click"
KIND_HOVER = "hover"
KIND_TYPE_TEXT = "type_text"
KIND_KEY_PRESS = "key_press"
KIND_HOLD_KEY = "hold_key"
KIND_SCROLL = "scroll"
KIND_DRAG = "drag"
KIND_SCREENSHOT = "screenshot"
KIND_INSPECT_TREE = "inspect_tree"
KIND_WAIT = "wait"
KIND_FOCUS_APP = "focus_app"


class ExecutorAdapter:
    name = "executor"

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg
        self._ex: Any = None

    def _ensure(self) -> Any:
        if self._ex is None:
            from fusion_executor import FusionSandboxExecutor
            log.info("executor connect sock=%s", self.cfg.executor_sock)
            self._ex = FusionSandboxExecutor(sock_path=self.cfg.executor_sock)
        return self._ex

    async def _run(self, action: dict) -> dict:
        """Offload the blocking gui_action to a thread; timeout + one retry.

        E4: the module docstring promises "one retry" but the old impl made a
        single attempt. Now it retries once on timeout/exception (matching the
        docstring) so a transient UDS hiccup does not fail the whole step.
        """
        raw_timeout = self.cfg.step_timeout_ms / 1000.0
        timeout = max(raw_timeout, 5.0)
        if timeout > raw_timeout:
            log.warning("executor step_timeout_ms=%d clamped to %.1fs floor", self.cfg.step_timeout_ms, timeout)
        last_err = None
        for attempt in range(2):  # initial + one retry
            def _do() -> dict:
                res = self._ensure().gui_action(action)
                return {"ok": res.ok, "error": res.error, "kind": action.get("kind")}
            try:
                res = await asyncio.wait_for(asyncio.to_thread(_do), timeout=timeout)
                if not res.get("ok"):
                    log.warning("executor %s failed (attempt %d): %s", action.get("kind"), attempt + 1, res.get("error"))
                    last_err = res.get("error")
                    # a logical failure (e.g. "no such element") is not going to
                    # be fixed by retrying — only retry transient transport errors.
                    return res
                return res
            except TimeoutError:
                last_err = "executor timeout"
                log.error("executor %s timed out after %.1fs (attempt %d)", action.get("kind"), timeout, attempt + 1)
                self._ex = None
            except Exception as e:
                last_err = str(e)
                log.error("executor %s raised (attempt %d): %s", action.get("kind"), attempt + 1, e)
                self._ex = None  # broken connection; force reconnect next call
        return {"ok": False, "error": last_err or "executor failed", "kind": action.get("kind")}

    async def screenshot(self) -> Screenshot:
        res = await self._gui_result(KIND_SCREENSHOT)
        if not res.ok:
            log.error("executor screenshot failed: %s", res.error)
        return Screenshot(
            png_b64=res.screenshot_png_b64,
            width=res.screenshot_width,
            height=res.screenshot_height,
            scale_factor=self.cfg.scale_factor,
            node_tree=None,
        )

    async def inspect_tree(self) -> Screenshot:
        res = await self._gui_result(KIND_INSPECT_TREE)
        return Screenshot(
            png_b64=res.screenshot_png_b64,
            width=res.screenshot_width,
            height=res.screenshot_height,
            scale_factor=self.cfg.scale_factor,
            node_tree=res.node_tree,
        )

    async def _gui_result(self, kind: str) -> Any:
        """Raw GuiResult for screenshot/inspect_tree (need attribute access).

        E5: inspect_tree walks the full AX hierarchy and on a complex window
        (hundreds of nodes) routinely exceeds the 5s click timeout. Give it an
        independent, longer budget so AX traversal is not misclassified as a
        timeout failure that permanently demotes perception to plain screenshot.
        R5: a single attempt let an instantaneous UDS hiccup fail the whole
        perception cycle (no screenshot → visual locate has no image). Retry
        once on transient transport error so a blip does not abort locate.
        """
        if kind == KIND_INSPECT_TREE:
            timeout = max(self.cfg.inspect_timeout_ms / 1000.0, 5.0)
        else:
            timeout = max(self.cfg.step_timeout_ms / 1000.0, 5.0)
        action = {"kind": kind}
        last_err = None
        for attempt in range(2):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._ensure().gui_action, action), timeout=timeout
                )
            except TimeoutError:
                last_err = f"executor timeout: {kind}"
                log.error("executor %s timed out after %.1fs (attempt %d)", kind, timeout, attempt + 1)
                self._ex = None
            except Exception as e:
                last_err = str(e)
                log.error("executor %s raised (attempt %d): %s", kind, attempt + 1, e)
                self._ex = None
        return _GuiResultError(last_err or f"executor failed: {kind}")

    async def click(self, loc: Locator, button: str = "left") -> dict:
        x, y = self._to_pixel(loc)
        kind = {
            "left": KIND_CLICK,
            "right": KIND_RIGHT_CLICK,
            "double": KIND_DOUBLE_CLICK,
            "triple": KIND_TRIPLE_CLICK,
            "hover": KIND_HOVER,
        }.get(button, KIND_CLICK)
        return await self._run({"kind": kind, "at": [x, y]})

    async def type_text(self, text: str) -> dict:
        return await self._run({"kind": KIND_TYPE_TEXT, "text": text})

    async def key_press(self, key: str, modifiers: list[str] | None = None) -> dict:
        action: dict = {"kind": KIND_KEY_PRESS, "key": key}
        if modifiers:
            action["modifiers"] = modifiers
        return await self._run(action)

    async def scroll(self, loc: Locator, dx: float = 0.0, dy: float = 0.0) -> dict:
        x, y = self._to_pixel(loc)
        return await self._run({"kind": KIND_SCROLL, "at": [x, y], "delta": [dx, dy]})

    async def drag(self, src: Locator, dst: Locator) -> dict:
        x1, y1 = self._to_pixel(src)
        x2, y2 = self._to_pixel(dst)
        return await self._run({"kind": KIND_DRAG, "from": [x1, y1], "to": [x2, y2]})

    async def move_path(self, points: list[tuple[float, float]]) -> dict:
        """Step through Bezier waypoints via per-point hover (F3.1 execution).

        Reduced waypoint set (D10 fix): upstream E2 will give batch-move; until
        then a small human-like path (~6 points) keeps round-trips bounded and
        the loop responsive. ok=False if any step fails.
        R3: a whole-path budget (cfg.move_path_timeout_ms) bounds the total; each
        per-point hover already has its own 5s timeout but 24 points × 5s = 120s
        worst case with no overall deadline could hang the Agent. Exceeding the
        path budget stops the move and returns the last failure.
        """
        import time as _time

        deadline = _time.monotonic() + (self.cfg.move_path_timeout_ms / 1000.0)
        last = {"ok": True, "error": None, "kind": "move_path"}
        for pt in points:
            if _time.monotonic() > deadline:
                log.error("move_path exceeded whole-path budget %dms — stopping", self.cfg.move_path_timeout_ms)
                return {"ok": False, "error": "move_path timeout", "kind": "move_path"}
            x, y = points_to_pixels(pt[0], pt[1], self.cfg.scale_factor)
            last = await self._run({"kind": KIND_HOVER, "at": [x, y]})
            if not last.get("ok"):
                log.warning("move_path stopped at %s: %s", pt, last.get("error"))
                return last
        return last

    async def wait(self, seconds: float) -> dict:
        return await self._run({"kind": KIND_WAIT, "seconds": seconds})

    async def focus_app(self, bundle_or_name: str) -> dict:
        return await self._run({"kind": KIND_FOCUS_APP, "target": bundle_or_name})

    def _to_pixel(self, loc: Locator) -> tuple[float, float]:
        x, y = loc.as_point()
        return points_to_pixels(x, y, self.cfg.scale_factor)

    async def close(self) -> None:
        ex = self._ex
        self._ex = None
        if ex is not None and hasattr(ex, "close"):
            try:
                await asyncio.to_thread(ex.close)
            except Exception as e:
                log.warning("executor close raised: %s", e)
        log.info("executor adapter closed")


class _GuiResultError:
    """Stand-in GuiResult when the call fails, so attribute access is safe."""

    ok = False
    error = "executor error"
    screenshot_png_b64 = None
    screenshot_width = None
    screenshot_height = None
    node_tree = None

    def __init__(self, error: str) -> None:
        self.error = error


class StubExecutorAdapter:
    name = "executor-stub"

    def __init__(self, cfg: OsaConfig, tree: str | None = None) -> None:
        self.cfg = cfg
        self.calls: list[dict] = []
        self._shot = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        self._tree = tree or '{"role":"AXWindow","children":[{"role":"AXButton","label":"OK","frame":[0,0,40,20]}]}'
        log.info("stub executor ready")

    async def screenshot(self) -> Screenshot:
        self.calls.append({"kind": KIND_SCREENSHOT})
        return Screenshot(png_b64=self._shot, width=1440, height=900, scale_factor=2.0, node_tree=None)

    async def inspect_tree(self) -> Screenshot:
        self.calls.append({"kind": KIND_INSPECT_TREE})
        return Screenshot(png_b64=self._shot, width=1440, height=900, scale_factor=2.0, node_tree=self._tree)

    async def click(self, loc: Locator, button: str = "left") -> dict:
        x, y = points_to_pixels(*loc.as_point(), self.cfg.scale_factor)
        rec = {"kind": "click", "button": button, "at": [x, y]}
        self.calls.append(rec)
        return {"ok": True, "error": None, "kind": "click"}

    async def type_text(self, text: str) -> dict:
        self.calls.append({"kind": KIND_TYPE_TEXT, "text": text})
        return {"ok": True, "error": None, "kind": KIND_TYPE_TEXT}

    async def key_press(self, key: str, modifiers: list[str] | None = None) -> dict:
        self.calls.append({"kind": KIND_KEY_PRESS, "key": key, "modifiers": modifiers or []})
        return {"ok": True, "error": None, "kind": KIND_KEY_PRESS}

    async def scroll(self, loc: Locator, dx: float = 0.0, dy: float = 0.0) -> dict:
        x, y = points_to_pixels(*loc.as_point(), self.cfg.scale_factor)
        self.calls.append({"kind": KIND_SCROLL, "at": [x, y], "delta": [dx, dy]})
        return {"ok": True, "error": None, "kind": KIND_SCROLL}

    async def drag(self, src: Locator, dst: Locator) -> dict:
        x1, y1 = points_to_pixels(*src.as_point(), self.cfg.scale_factor)
        x2, y2 = points_to_pixels(*dst.as_point(), self.cfg.scale_factor)
        self.calls.append({"kind": KIND_DRAG, "from": [x1, y1], "to": [x2, y2]})
        return {"ok": True, "error": None, "kind": KIND_DRAG}

    async def move_path(self, points: list[tuple[float, float]]) -> dict:
        for pt in points:
            x, y = points_to_pixels(pt[0], pt[1], self.cfg.scale_factor)
            self.calls.append({"kind": KIND_HOVER, "at": [x, y]})
        return {"ok": True, "error": None, "kind": "move_path"}

    async def wait(self, seconds: float) -> dict:
        self.calls.append({"kind": KIND_WAIT, "seconds": seconds})
        return {"ok": True, "error": None, "kind": KIND_WAIT}

    async def focus_app(self, bundle_or_name: str) -> dict:
        self.calls.append({"kind": KIND_FOCUS_APP, "target": bundle_or_name})
        return {"ok": True, "error": None, "kind": KIND_FOCUS_APP}

    async def close(self) -> None:
        log.info("stub executor closed")
