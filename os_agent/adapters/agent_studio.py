"""fusion-agent-studio adapter: orchestration host (AgentGraph/Runtime/ToolRegistry).

osagent is a barrier layer, not a replacement for agent-studio. This adapter lets
osagent hand a decomposed plan to agent-studio's runtime over HTTP, and read back
the 37 built-in tools registry. Phase 0 only needs the tool-list + a thin execute
bridge; deeper AgentGraph integration is Phase 2 (pending upstream issues A1/A2/A3).
"""
from __future__ import annotations

from fusion_core import get_async_client, get_logger

from os_agent.config import OsaConfig

log = get_logger("os_agent.adapters.agent_studio")


class AgentStudioAdapter:
    name = "agent-studio"

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg
        self._base = cfg.agent_studio_url.rstrip("/")

    async def list_tools(self) -> list[dict]:
        client = get_async_client(self._base, timeout=10)
        try:
            r = await client.get("/v1/tools")
            data = r.json()
            tools = data.get("tools", data if isinstance(data, list) else [])
            log.info("agent-studio tools=%d", len(tools))
            return tools
        except Exception as e:
            log.warning("agent-studio list_tools failed: %s", e)
            return []

    async def run_graph(self, plan: list[dict]) -> dict:
        client = get_async_client(self._base, timeout=60)
        try:
            r = await client.post("/v1/graph/run", json={"nodes": plan})
            return r.json()
        except Exception as e:
            log.error("agent-studio run_graph failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def close(self) -> None:
        log.info("agent-studio adapter closed")


class StubAgentStudioAdapter:
    name = "agent-studio-stub"

    def __init__(self, cfg: OsaConfig) -> None:
        self.cfg = cfg
        self.calls: list[dict] = []
        self._tools = [
            {"name": "screen_capture", "kind": "computer_use"},
            {"name": "mouse", "kind": "computer_use"},
            {"name": "keyboard", "kind": "computer_use"},
            {"name": "clipboard", "kind": "computer_use"},
        ]
        log.info("stub agent-studio ready")

    async def list_tools(self) -> list[dict]:
        self.calls.append({"method": "list_tools"})
        return self._tools

    async def run_graph(self, plan: list[dict]) -> dict:
        self.calls.append({"method": "run_graph", "nodes": len(plan)})
        return {"ok": True, "steps": len(plan)}

    async def close(self) -> None:
        log.info("stub agent-studio closed")
