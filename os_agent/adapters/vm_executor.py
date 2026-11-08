"""fusion-autotest VM-fleet adapter: host-safe execution backend (issue #9).

mode=vm delegates actuation to a disposable macOS guest driven through the
fusion-autotest VM fleet (issue #37, terminal-mac pattern): an in-guest helper
captures the screen and posts input events, so the host pointer, clipboard,
and signed-in sessions are never exposed to the agent.

Capability ceiling (documented honestly, mirrored from the spike report):
- No host AX tree: the guest helper exposes pixels + input events only, so
  inspect_tree returns an AX-less Screenshot and perception runs the visual
  track exclusively.
- No host apps: focus_app is refused (there is no host app surface).
- Vertical scroll only: the guest helper takes dy; a dx component is refused.
- No human-like cursor path: move_path is a no-op — the trajectory module
  exists to keep the HOST cursor from teleporting; a guest has no observer.
- Coordinates are guest points (scale_factor 1.0 at the API boundary).

The fleet client is synchronous (blocking mailbox round-trips). Every call is
offloaded to a worker thread via asyncio.to_thread with the standard per-call
timeout, same discipline as ExecutorAdapter.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from fusion_core import get_logger

from os_agent.adapters.base import Locator, Screenshot
from os_agent.config import OsaConfig

log = get_logger("os_agent.adapters.vm_executor")


class VmExecutorAdapter:
    name = "vm-executor"

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg
        self._fleet: Any = None

    def _ensure(self) -> Any:
        if self._fleet is None:
            from fusion_autotest.vm.fleet import VMFleet

            self._fleet = VMFleet()
            if not self._fleet.exists(self.cfg.vm_name):
                raise RuntimeError(
                    f"VM '{self.cfg.vm_name}' not found in fleet — provision it with "
                    "fusion-autotest scripts/vm_spike.py provision, or set OSA_VM_NAME"
                )
        return self._fleet

    def _ensure_helper(self) -> Any:
        """Connect + wait for the in-guest helper (idempotent, off-call).

        Raises with an actionable message when the one-time guided golden prep
        (setup assistant, auto-login, TCC grants) has not been done — the
        documented upstream gap in docs/vm-sandbox-spike-report.md.
        """
        fleet = self._ensure()
        if not fleet.is_running(self.cfg.vm_name):
            fleet.start(self.cfg.vm_name)
        return fleet.wait_helper(self.cfg.vm_name)

    async def _run(self, fn: Any, timeout_s: float) -> dict:
        try:
            values = await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout_s)
            ok = not (isinstance(values, dict) and str(values.get("ok", "true")).lower() == "false")
            return {"ok": ok, "error": None if ok else "vm helper reported failure", "kind": "vm"}
        except TimeoutError:
            log.error("vm action timed out after %.1fs", timeout_s)
            return {"ok": False, "error": "vm action timeout", "kind": "vm"}
        except Exception as e:
            log.error("vm action raised: %s", e)
            return {"ok": False, "error": str(e), "kind": "vm"}

    async def screenshot(self) -> Screenshot:
        def _do() -> tuple[dict, bytes]:
            self._ensure_helper()
            return self._ensure().screenshot(self.cfg.vm_name)

        try:
            values, data = await asyncio.wait_for(asyncio.to_thread(_do), timeout=30.0)
        except Exception as e:
            log.error("vm screenshot failed: %s", e)
            return Screenshot(png_b64=None, width=None, height=None, scale_factor=1.0, node_tree=None)
        w = int(values.get("pointWidth") or 0) or None
        h = int(values.get("pointHeight") or 0) or None
        return Screenshot(
            png_b64=base64.b64encode(data).decode("ascii"),
            width=w,
            height=h,
            scale_factor=1.0,  # guest coordinates are already points
            node_tree=None,  # no AX tree inside the guest (capability ceiling)
            meta={"sandbox": "vm", "vm": self.cfg.vm_name},
        )

    async def inspect_tree(self) -> Screenshot:
        # No AX in the guest by design (attack-surface minimalism). Perception
        # degrades to the visual track on has_ax=False.
        log.info("vm inspect_tree: guest exposes no AX tree, returning visual-only frame")
        return await self.screenshot()

    async def click(self, loc: Locator, button: str = "left") -> dict:
        x, y = loc.as_point()

        def _do() -> dict:
            self._ensure_helper()
            return self._ensure().click(
                self.cfg.vm_name, int(x), int(y), button="left" if button in ("left", "hover") else button
            )

        return await self._run(_do, timeout_s=max(self.cfg.step_timeout_ms / 1000.0, 15.0))

    async def type_text(self, text: str) -> dict:

        def _do() -> dict:
            self._ensure_helper()
            return self._ensure().type_text(self.cfg.vm_name, text)

        return await self._run(_do, timeout_s=max(self.cfg.step_timeout_ms / 1000.0, 30.0))

    async def key_press(self, key: str, modifiers: list[str] | None = None) -> dict:

        def _do() -> dict:
            self._ensure_helper()
            chord = "+".join([*(modifiers or []), key]).lower()
            return self._ensure().press_key(self.cfg.vm_name, chord)

        return await self._run(_do, timeout_s=max(self.cfg.step_timeout_ms / 1000.0, 15.0))

    async def scroll(self, loc: Locator, dx: float = 0.0, dy: float = 0.0) -> dict:
        if abs(dx) > 0:
            log.warning("vm scroll refuses dx=%s: guest helper supports vertical scroll only", dx)
            return {"ok": False, "error": "vm scroll supports dy only", "kind": "scroll"}
        x, y = loc.as_point()

        def _do() -> dict:
            self._ensure_helper()
            return self._ensure().scroll(self.cfg.vm_name, int(dy), x=int(x), y=int(y))

        return await self._run(_do, timeout_s=max(self.cfg.step_timeout_ms / 1000.0, 15.0))

    async def drag(self, src: Locator, dst: Locator) -> dict:
        x1, y1 = src.as_point()
        x2, y2 = dst.as_point()

        def _do() -> dict:
            self._ensure_helper()
            return self._ensure().drag(self.cfg.vm_name, int(x1), int(y1), int(x2), int(y2))

        return await self._run(_do, timeout_s=max(self.cfg.step_timeout_ms / 1000.0, 30.0))

    async def move_path(self, points: list[tuple[float, float]]) -> dict:
        # Capability ceiling: the human-like Bezier path exists to keep the HOST
        # cursor from teleporting (anti-fingerprint, UX). A disposable guest has
        # no observer and no host cursor — skip the wayponts, log for honesty.
        log.info("vm move_path: no-op (%d waypoints skipped, guest has no host cursor)", len(points))
        return {"ok": True, "error": None, "kind": "move_path"}

    async def wait(self, seconds: float) -> dict:
        await asyncio.sleep(seconds)
        return {"ok": True, "error": None, "kind": "wait"}

    async def focus_app(self, bundle_or_name: str) -> dict:
        # Capability ceiling: vm mode has no host app surface to focus.
        log.warning("vm focus_app refused: %r (no host apps in sandbox)", bundle_or_name)
        return {"ok": False, "error": "focus_app unavailable in vm sandbox", "kind": "focus_app"}

    async def close(self) -> None:
        # Disposable fleet state: leave the guest running for inspection, same
        # semantics as VMDriver.disconnect. Callers wanting a fresh desktop
        # call VMFleet.restore() before the next run.
        self._fleet = None
        log.info("vm executor adapter closed (guest left running)")

    async def health(self) -> bool:
        try:
            await asyncio.wait_for(asyncio.to_thread(self._ensure_helper), timeout=30.0)
            return True
        except Exception as e:
            log.warning("vm health failed: %s", e)
            return False


class StubVmExecutorAdapter:
    name = "vm-executor-stub"

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg
        self.calls: list[dict] = []
        self._shot = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"

    async def screenshot(self) -> Screenshot:
        self.calls.append({"kind": "screenshot"})
        return Screenshot(png_b64=self._shot, width=1440, height=900, scale_factor=1.0, node_tree=None)

    async def inspect_tree(self) -> Screenshot:
        self.calls.append({"kind": "inspect_tree"})
        return await self.screenshot()

    async def click(self, loc: Locator, button: str = "left") -> dict:
        self.calls.append({"kind": "click", "at": list(loc.as_point()), "button": button})
        return {"ok": True, "error": None, "kind": "click"}

    async def type_text(self, text: str) -> dict:
        self.calls.append({"kind": "type_text", "text": text})
        return {"ok": True, "error": None, "kind": "type_text"}

    async def key_press(self, key: str, modifiers: list[str] | None = None) -> dict:
        self.calls.append({"kind": "key_press", "key": key, "modifiers": modifiers or []})
        return {"ok": True, "error": None, "kind": "key_press"}

    async def scroll(self, loc: Locator, dx: float = 0.0, dy: float = 0.0) -> dict:
        self.calls.append({"kind": "scroll", "dx": dx, "dy": dy})
        if abs(dx) > 0:
            return {"ok": False, "error": "vm scroll supports dy only", "kind": "scroll"}
        return {"ok": True, "error": None, "kind": "scroll"}

    async def drag(self, src: Locator, dst: Locator) -> dict:
        self.calls.append({"kind": "drag", "from": list(src.as_point()), "to": list(dst.as_point())})
        return {"ok": True, "error": None, "kind": "drag"}

    async def move_path(self, points: list[tuple[float, float]]) -> dict:
        self.calls.append({"kind": "move_path", "points": len(points), "noop": True})
        return {"ok": True, "error": None, "kind": "move_path"}

    async def wait(self, seconds: float) -> dict:
        self.calls.append({"kind": "wait", "seconds": seconds})
        return {"ok": True, "error": None, "kind": "wait"}

    async def focus_app(self, bundle_or_name: str) -> dict:
        self.calls.append({"kind": "focus_app", "target": bundle_or_name})
        return {"ok": False, "error": "focus_app unavailable in vm sandbox", "kind": "focus_app"}

    async def close(self) -> None:
        log.info("stub vm executor closed")

    async def health(self) -> bool:
        return True
