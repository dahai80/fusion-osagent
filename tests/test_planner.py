"""Planner FSM tests — guard pass, guard fail halt, full run, heal-retry."""
from __future__ import annotations

import pytest

from os_agent.adapters.base import Screenshot
from os_agent.planner import GuardResult, Plan, Planner, PlanStatus, Step

SHOT = Screenshot(png_b64="x", width=10, height=10, scale_factor=2.0)


def _guard_ok(_s, _st):
    return GuardResult(ok=True, reason="ok")


def _guard_fail(_s, _st):
    return GuardResult(ok=False, reason="dialog not open")


async def _execute_ok(step, shot, plan):
    plan.state.setdefault("done", []).append(step.name)
    return True


async def _execute_fail(step, shot, plan):
    return False


async def _capture():
    return SHOT


@pytest.mark.asyncio
async def test_plan_runs_all_steps_when_guards_pass():
    plan = Plan(name="p", steps=[
        Step(name="s1", action="click", target="OK", guard=_guard_ok),
        Step(name="s2", action="type", target="field", guard=_guard_ok),
    ])
    pl = Planner()
    await pl.run(plan, _capture, _execute_ok)
    assert plan.status == PlanStatus.DONE
    assert plan.cursor == 2
    assert plan.state["done"] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_plan_halts_on_guard_fail():
    plan = Plan(name="p", steps=[
        Step(name="s1", action="click", target="OK", guard=_guard_fail),
    ])
    pl = Planner(max_retries=0)
    await pl.run(plan, _capture, _execute_ok)
    assert plan.status == PlanStatus.HALTED
    assert plan.state["halt_step"] == "s1"
    assert "dialog not open" in plan.state["halt_reason"]


@pytest.mark.asyncio
async def test_plan_halts_on_action_fail():
    plan = Plan(name="p", steps=[
        Step(name="s1", action="click", target="OK", guard=_guard_ok),
    ])
    pl = Planner(max_retries=0)
    await pl.run(plan, _capture, _execute_fail)
    assert plan.status == PlanStatus.HALTED
    assert plan.state["halt_reason"] == "action failed"


@pytest.mark.asyncio
async def test_plan_heal_retry_recovers():
    calls = {"n": 0}

    def _guard_retry(_s, _st):
        calls["n"] += 1
        if calls["n"] == 1:
            return GuardResult(ok=False, reason="not ready")
        return GuardResult(ok=True, reason="ready now")

    plan = Plan(name="p", steps=[
        Step(name="s1", action="click", target="OK", guard=_guard_retry),
    ])
    pl = Planner(max_retries=1)
    await pl.run(plan, _capture, _execute_ok)
    assert plan.status == PlanStatus.DONE
    assert plan.cursor == 1


@pytest.mark.asyncio
async def test_plan_empty_is_done():
    plan = Plan(name="empty", steps=[])
    pl = Planner()
    await pl.run(plan, _capture, _execute_ok)
    assert plan.status == PlanStatus.DONE


@pytest.mark.asyncio
async def test_remaining_steps():
    plan = Plan(name="p", steps=[
        Step(name="s1", action="click", target="A"),
        Step(name="s2", action="type", target="B"),
    ])
    plan.cursor = 1
    rem = plan.remaining()
    assert [s.name for s in rem] == ["s2"]
