"""fusion-browser adapter: Web AXTree + screenshot + visual grounding.

Upstream issue B2 (fusion-browser#8) blocks an official Python client. Until then
this adapter speaks the UDS JSON-RPC schema directly (createSession/execute/close)
and degrades to stub in tests. Visual grounding is click-centroid only upstream
(issue B1 for full-screen BBox); SOM on Web falls back to mlx VLM detection.
"""
from __future__ import annotations

import json
import socket
import struct

from fusion_core import get_logger

from os_agent.adapters.base import Screenshot
from os_agent.config import OsaConfig

log = get_logger("os_agent.adapters.browser")

CAP_SCREENSHOT = 1 << 4
CAP_CLICK = 1 << 1
CAP_TYPE = 1 << 2
CAP_SCROLL = 1 << 3
CAP_NAVIGATE = 1 << 0


def _lenprefixed(sock: socket.socket, payload: bytes) -> bytes:
    sock.sendall(struct.pack(">I", len(payload)) + payload)
    header = _recvn(sock, 4)
    if header is None:
        return b""
    (n,) = struct.unpack(">I", header)
    return _recvn(sock, n) or b""


def _recvn(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


class BrowserAdapter:
    name = "browser"

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg
        self._session: str | None = None

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        msg = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            msg["params"] = params
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.cfg.browser_sock)
            log.info("browser rpc %s sock=%s", method, self.cfg.browser_sock)
            raw = _lenprefixed(s, json.dumps(msg).encode())
        if not raw:
            log.warning("browser rpc %s empty reply", method)
            return {}
        try:
            return json.loads(raw).get("result", {})
        except json.JSONDecodeError as e:
            log.error("browser rpc %s decode failed: %s", method, e)
            return {}

    async def screenshot(self) -> Screenshot:
        if self._session is None:
            self._session = self._rpc("createSession", {"capabilities": CAP_SCREENSHOT}).get("sessionId")
        res = self._rpc("execute", {"sessionId": self._session, "action": {"kind": "screenshot"}})
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
        if self._session is None:
            self._session = self._rpc("createSession", {"capabilities": CAP_CLICK | CAP_SCREENSHOT}).get("sessionId")
        return self._rpc("execute", {"sessionId": self._session, "action": {"kind": "click", "visual": query}})

    async def navigate(self, url: str) -> dict:
        if self._session is None:
            self._session = self._rpc("createSession", {"capabilities": CAP_NAVIGATE}).get("sessionId")
        return self._rpc("execute", {"sessionId": self._session, "action": {"kind": "navigate", "url": url}})

    async def close(self) -> None:
        if self._session:
            self._rpc("close", {"sessionId": self._session})
            self._session = None
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
