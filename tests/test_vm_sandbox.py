"""Regression tests for the sandbox=vm execution backend (issue #9)."""

from __future__ import annotations

from os_agent.adapters.base import Locator
from os_agent.adapters.vm_executor import StubVmExecutorAdapter
from os_agent.api import DesktopAgent
from os_agent.config import OsaConfig
from os_agent.sandbox import recommended_sandbox_mode


# config: sandbox_mode validation
def test_sandbox_mode_default_host():
    cfg = OsaConfig()
    assert cfg.sandbox_mode == "host"
    assert cfg.vm_name == "golden"


def test_sandbox_mode_unknown_falls_back_to_host():
    cfg = OsaConfig(sandbox_mode="container")
    assert cfg.sandbox_mode == "host"


def test_sandbox_mode_vm_accepted():
    cfg = OsaConfig(sandbox_mode="vm", vm_name="flea")
    assert cfg.sandbox_mode == "vm"
    assert cfg.vm_name == "flea"


def test_sandbox_mode_empty_vm_name_falls_back():
    cfg = OsaConfig(vm_name="")
    assert cfg.vm_name == "golden"


# policy hook: credential/identity signals route to vm by default
def test_policy_flags_credentials_to_vm():
    assert recommended_sandbox_mode("log into the admin console with the password from the doc") == "vm"
    assert recommended_sandbox_mode("paste the API key into the settings form") == "vm"
    assert recommended_sandbox_mode("copy the ssh key to the server") == "vm"
    assert recommended_sandbox_mode("enter the credit card number at checkout") == "vm"
    assert recommended_sandbox_mode("read the otp from messages and type it") == "vm"


def test_policy_keeps_benign_tasks_on_host():
    assert recommended_sandbox_mode("open the calendar and create an event on Friday") == "host"
    assert recommended_sandbox_mode("take a screenshot of the dashboard") == "host"
    assert recommended_sandbox_mode("") == "host"
    assert recommended_sandbox_mode(None) == "host"


# DesktopAgent wiring: vm mode swaps the executor, stub mode stays offline
def test_desktop_agent_vm_stub_uses_vm_stub_executor():
    agent = DesktopAgent(OsaConfig(stub_mode=True, sandbox_mode="vm"))
    assert isinstance(agent.executor, StubVmExecutorAdapter)
    assert agent.executor.name == "vm-executor-stub"


def test_desktop_agent_host_mode_unchanged():
    agent = DesktopAgent(OsaConfig(stub_mode=True, sandbox_mode="host"))
    assert agent.executor.name == "executor-stub"


# stub vm executor contract: capability ceiling is behavior, not just docs
async def test_vm_stub_capability_ceiling():
    ex = StubVmExecutorAdapter(OsaConfig(stub_mode=True, sandbox_mode="vm"))
    shot = await ex.screenshot()
    assert shot.node_tree is None, "guest exposes no AX tree"
    assert shot.scale_factor == 1.0, "guest coordinates are points"
    assert (await ex.focus_app("Safari"))["ok"] is False, "no host apps in vm sandbox"
    assert (await ex.scroll(Locator(kind="point", x=10, y=10), dx=5, dy=0))["ok"] is False, "dy-only scroll"
    assert (await ex.move_path([(1, 1), (2, 2)]))["ok"] is True, "move_path is an honest no-op"
    kinds = [c["kind"] for c in ex.calls]
    assert kinds[-1] == "move_path" and ex.calls[-1]["noop"] is True
