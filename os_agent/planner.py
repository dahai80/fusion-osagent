"""FSM planner + State Guard (PRD F2.3 / Phase 1.4).

A Plan is an ordered list of Steps. Each Step has a guard predicate checked
before the action runs; if the guard fails the plan halts (State Guard) so a
bad state never cascades. The FSM tracks the current step index and the
running state, enabling resume after a heal.

Guard predicates take the latest Screenshot + plan state and return
(ok, reason). Guards encode "preconditions": e.g. "dialog must be open",
"target element must be visible". A failed guard triggers heal-then-retry
once; a second failure halts (Rule 12: fail visibly).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from fusion_core import get_logger

from os_agent.adapters.base import Screenshot

log = get_logger("os_agent.planner")


class PlanStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    HALTED = "halted"


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""


@dataclass
class Step:
    name: str
    action: str
    target: str = ""
    guard: object = None  # callable(Screenshot, dict) -> GuardResult
    meta: dict = field(default_factory=dict)


@dataclass
class Plan:
    name: str
    steps: list[Step] = field(default_factory=list)
    cursor: int = 0
    status: PlanStatus = PlanStatus.PENDING
    state: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    def remaining(self) -> list[Step]:
        return self.steps[self.cursor:]


def always_true(_shot: Screenshot, _state: dict) -> GuardResult:
    return GuardResult(ok=True, reason="no guard")


class Planner:
    """Runs a Plan step-by-step with State Guard + one heal-retry."""

    def __init__(self, max_retries: int = 1) -> None:
        self.max_retries = max_retries

    def check_guard(self, step: Step, shot: Screenshot, state: dict) -> GuardResult:
        guard = step.guard or always_true
        try:
            res = guard(shot, state)
            if isinstance(res, GuardResult):
                return res
            if isinstance(res, tuple):
                return GuardResult(ok=bool(res[0]), reason=str(res[1]) if len(res) > 1 else "")
            return GuardResult(ok=bool(res))
        except Exception as e:
            log.error("guard %s raised: %s", step.name, e)
            return GuardResult(ok=False, reason=f"guard error: {e}")

    async def advance(self, plan: Plan, shot: Screenshot, execute) -> Plan:
        """Execute next step. `execute(step, shot, plan) -> bool` returns action ok."""
        if plan.status in (PlanStatus.DONE, PlanStatus.HALTED):
            log.info("plan %s already %s — skip", plan.name, plan.status.value)
            return plan
        plan.status = PlanStatus.RUNNING
        if plan.cursor >= len(plan.steps):
            plan.status = PlanStatus.DONE
            log.info("plan %s done", plan.name)
            return plan

        step = plan.steps[plan.cursor]
        guard_res = self.check_guard(step, shot, plan.state)
        plan.history.append({"step": step.name, "cursor": plan.cursor, "guard": guard_res.ok, "reason": guard_res.reason})

        if not guard_res.ok:
            log.warning("guard failed at step %s: %s — halt", step.name, guard_res.reason)
            plan.status = PlanStatus.HALTED
            plan.state["halt_step"] = step.name
            plan.state["halt_reason"] = guard_res.reason
            return plan

        ok = False
        try:
            ok = await execute(step, shot, plan)
        except Exception as e:
            # B17: an execute exception used to bubble out of run() and leave
            # the plan in RUNNING with a history entry missing action_ok.
            # Catch, record the failure loudly, and halt the plan so state is
            # consistent and consumers never KeyError on action_ok.
            log.error("execute %s raised at step %s: %s", step.action, step.name, e, exc_info=True)
            plan.history[-1]["execute_error"] = str(e)
        plan.history[-1]["action_ok"] = ok
        if not ok:
            log.warning("action %s failed at step %s", step.action, step.name)
            plan.status = PlanStatus.HALTED
            plan.state["halt_step"] = step.name
            plan.state["halt_reason"] = "action failed"
            return plan

        plan.cursor += 1
        if plan.cursor >= len(plan.steps):
            plan.status = PlanStatus.DONE
            log.info("plan %s done after step %s", plan.name, step.name)
        return plan

    async def run(self, plan: Plan, capture, execute, heal=None) -> Plan:
        """Run all remaining steps: capture before each, advance, heal-retry once on halt.

        D8 fix: when a step halts the plan and a `heal` callback is supplied
        (heal(plan, shot) -> bool), the heal is actually invoked before retry;
        without heal the old re-advance-same-step behavior is kept. A second
        halt after exhausting retries halts for good (Rule 12).
        """
        retries = 0
        while plan.status in (PlanStatus.RUNNING, PlanStatus.PENDING):
            shot = await capture()
            await self.advance(plan, shot, execute)
            if plan.status == PlanStatus.HALTED and retries < self.max_retries:
                retries += 1
                healed = False
                if heal is not None:
                    try:
                        healed = bool(await heal(plan, shot))
                    except Exception as e:
                        log.error("heal callback raised: %s", e)
                        healed = False
                    log.info("plan %s halted, heal %s retry %d/%d", plan.name, "ok" if healed else "failed", retries, self.max_retries)
                else:
                    log.info("plan %s halted, no heal — re-advance retry %d/%d", plan.name, retries, self.max_retries)
                if healed:
                    plan.state.pop("halt_step", None)
                    plan.state.pop("halt_reason", None)
                    # R3: a successful heal means the halt was transient — give
                    # the plan its full retry budget back so a subsequent
                    # genuine failure is not mistaken for "retries exhausted".
                    retries = 0
                plan.status = PlanStatus.RUNNING
                continue
            if plan.status in (PlanStatus.DONE, PlanStatus.HALTED):
                break
        return plan
