"""fusion-browser adapter: Web AXTree + screenshot + visual grounding.

Upstream issue B2 (fusion-browser#8) blocks an official Python client. Until then
this adapter speaks the UDS JSON-RPC schema directly (createSession/execute/close)
and degrades to stub in tests. Visual grounding is click-centroid only upstream
(issue B1 for full-screen BBox); SOM on Web falls back to mlx VLM detection.

Connection model (D2 fix): one persistent UDS socket reused across RPCs, a
single session created with the union of needed capabilities, an atomic
request-id counter (no hardcoded id=1 collision), retry on transient failure,
and a real close() that closes the session + socket.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import socket
import struct
import threading

from fusion_core import get_logger

from os_agent.adapters.base import Screenshot
from os_agent.config import OsaConfig

log = get_logger("os_agent.adapters.browser")

CAP_SCREENSHOT = 1 << 4
CAP_CLICK = 1 << 1
CAP_TYPE = 1 << 2
CAP_SCROLL = 1 << 3
CAP_NAVIGATE = 1 << 0
CAP_ALL = CAP_SCREENSHOT | CAP_CLICK | CAP_TYPE | CAP_SCROLL | CAP_NAVIGATE

RPC_TIMEOUT = 10.0
RPC_RETRIES = 2


def _recvn(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _send_recv(sock: socket.socket, payload: bytes) -> bytes:
    sock.sendall(struct.pack(">I", len(payload)) + payload)
    header = _recvn(sock, 4)
    if header is None:
        return b""
    (n,) = struct.unpack(">I", header)
    return _recvn(sock, n) or b""


class BrowserAdapter:
    name = "browser"

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg
        self._session: str | None = None
        self._sock: socket.socket | None = None
        self._ids = itertools.count(1)
        # F1/P0: single UDS socket + concurrent RPCs interleave length-prefix
        # frames and corrupt the stream. Serialise all send/recv under one lock.
        self._rpc_lock = threading.Lock()

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(RPC_TIMEOUT)
        s.connect(self.cfg.browser_sock)
        self._sock = s
        log.info("browser socket connected sock=%s", self.cfg.browser_sock)
        return s

    def _rpc_sync(self, method: str, params: dict | None = None) -> dict | None:
        msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params:
            msg["params"] = params
        last_err: str = ""
        # A1: hold the lock across the whole retry loop so two concurrent
        # _rpc_sync calls never interleave send/recv on the shared socket.
        with self._rpc_lock:
            for _attempt in range(RPC_RETRIES + 1):
                try:
                    sock = self._connect()
                    raw = _send_recv(sock, json.dumps(msg).encode())
                    if not raw:
                        last_err = "empty reply"
                        self._reset_sock()
                        continue
                    resp = json.loads(raw)
                    if "error" in resp:
                        last_err = str(resp["error"])
                        continue
                    log.info("browser rpc %s ok", method)
                    return resp.get("result", {})
                except (OSError, json.JSONDecodeError) as e:
                    last_err = str(e)
                    self._reset_sock()
        log.error("browser rpc %s failed after %d retries: %s", method, RPC_RETRIES, last_err)
        return None

    def _reset_sock(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _ensure_session(self, caps: int) -> str | None:
        if self._session is not None:
            return self._session
        res = self._rpc_sync("createSession", {"capabilities": CAP_ALL | caps})
        if res is None:
            return None
        sid = res.get("sessionId")
        if sid:
            self._session = sid
        return sid

    async def _rpc(self, method: str, params: dict | None = None) -> dict | None:
        return await asyncio.to_thread(self._rpc_sync, method, params)

    async def screenshot(self) -> Screenshot:
        sid = self._ensure_session(CAP_SCREENSHOT)
        res = await self._rpc("execute", {"sessionId": sid, "action": {"kind": "screenshot"}}) if sid else None
        if not res:
            return Screenshot(png_b64=None, width=None, height=None, scale_factor=self.cfg.scale_factor, node_tree=None)
        png = res.get("screenshotPng")
        png_b64: str | None = None
        if isinstance(png, (bytes, bytearray)):
            import base64
            png_b64 = base64.b64encode(png).decode()
        elif isinstance(png, str):
            png_b64 = png
        return Screenshot(
            png_b64=png_b64,
            width=res.get("width"),
            height=res.get("height"),
            scale_factor=self.cfg.scale_factor,
            node_tree=res.get("axTree"),
        )

    async def visual_click(self, query: str) -> dict:
        sid = self._ensure_session(CAP_CLICK | CAP_SCREENSHOT)
        if not sid:
            return {"ok": False, "error": "no browser session"}
        res = await self._rpc("execute", {"sessionId": sid, "action": {"kind": "click", "visual": query}})
        return res or {"ok": False, "error": "browser rpc failed"}

    async def navigate(self, url: str) -> dict:
        sid = self._ensure_session(CAP_NAVIGATE)
        if not sid:
            return {"ok": False, "error": "no browser session"}
        res = await self._rpc("execute", {"sessionId": sid, "action": {"kind": "navigate", "url": url}})
        return res or {"ok": False, "error": "browser rpc failed"}

    async def close(self) -> None:
        if self._session:
            await self._rpc("close", {"sessionId": self._session})
            self._session = None
        self._reset_sock()
        log.info("browser adapter closed")


class StubBrowserAdapter:
    name = "browser-stub"

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg
        self.calls: list[dict] = []
        self._tree = '{"url":"about:blank","title":"stub","children":[{"role":"link","text":"Search"}]}'
        self._shot = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        log.info("stub browser ready")

    async def screenshot(self) -> Screenshot:
        self.calls.append({"kind": "screenshot"})
        return Screenshot(png_b64=self._shot, width=1440, height=900, scale_factor=2.0, node_tree=self._tree)

    async def visual_click(self, query: str) -> dict:
        self.calls.append({"kind": "visual_click", "query": query})
        return {"ok": True, "x": 720, "y": 450}

    async def navigate(self, url: str) -> dict:
        self.calls.append({"kind": "navigate", "url": url})
        return {"ok": True, "url": url}

    async def close(self) -> None:
        log.info("stub browser closed")
