"""Sibling capability adapters.

Each adapter wraps one sibling: executor (AX/CGEvent), browser (Web AXTree/CDP),
mlx (inference), agent-studio (orchestration). All expose a uniform async API and
ship a stub implementation for offline tests (OSA_STUB_MODE or injected in tests).
"""
from __future__ import annotations

from os_agent.adapters.agent_studio import AgentStudioAdapter, StubAgentStudioAdapter
from os_agent.adapters.base import Adapter, Locator, Screenshot
from os_agent.adapters.browser import BrowserAdapter, StubBrowserAdapter
from os_agent.adapters.executor import ExecutorAdapter, StubExecutorAdapter
from os_agent.adapters.mlx import MlxAdapter, StubMlxAdapter

__all__ = [
    "Adapter",
    "Locator",
    "Screenshot",
    "ExecutorAdapter",
    "StubExecutorAdapter",
    "MlxAdapter",
    "StubMlxAdapter",
    "BrowserAdapter",
    "StubBrowserAdapter",
    "AgentStudioAdapter",
    "StubAgentStudioAdapter",
]
