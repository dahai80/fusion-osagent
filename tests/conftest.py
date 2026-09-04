"""Test fixtures: stub adapters injected via OSA_STUB_MODE + direct injection.

Pattern migrated from fusion-robot tests/conftest.py (sys.modules stub at import
time). Here simpler: osagent stubs are in-process classes, no sys.modules hack
needed — tests just build DesktopAgent with stub_mode=True or inject stubs.
"""
from __future__ import annotations

import pytest

from os_agent.config import OsaConfig


@pytest.fixture
def cfg() -> OsaConfig:
    return OsaConfig(stub_mode=True)


@pytest.fixture
def event_recorder():
    records: list[dict] = []
    return records
