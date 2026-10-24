"""A fleet run's plan: what it will run, agreed before anything starts.

With `fleet.require_plan` on, a chat turn's fleet runs only under a plan its person
accepted (design: lanes/V2/FLEET-WIZARD-DESIGN.md). The plan is built from the
agent's fleet limits (`PlanCaps`) and the models the person named; the person may
change its values, never past those caps; every spawn then stays inside it.

Pure: no I/O. `daimon.core.stores.fleet_plans` keeps the rows; the MCP fleet tools
build plans, and the host accepts them.
"""

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_DOWN, Decimal

from daimon.core.errors import UserFacingError
from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The smallest budget a worker can be given (a cent), so a plan never rounds to 0.
_CENT = Decimal("0.01")


class PlanRefused(UserFacingError):
    """A plan, a change to one, or a spawn under one goes past what is allowed."""


class PlannedWorker(BaseModel):
    """One worker in the plan: a task on a model, or an on-server agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str = Field(min_length=1, max_length=200)
    task: str | None = Field(default=None, max_length=20000)
    # The granted model id it runs on (resolved, never a nickname), or None for an
    # on-server agent, which runs on its own model.
    model: str | None = Field(default=None, min_length=1, max_length=200)
    agent: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _one_kind(self) -> PlannedWorker:
        if (self.model is None) == (self.agent is None):
            raise ValueError("a planned worker names exactly one of model or agent")
        return self


class PlanCaps(BaseModel):
    """What a plan may go up to, from the agent's fleet limits (its grant and the
    frozen root). Raising these is the fleet settings' job, never the plan's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_workers: int = Field(gt=0)
    budget_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    worker_seconds: int = Field(gt=0)
    max_turns: int = Field(gt=0)
    models: tuple[str, ...] = Field(min_length=1)


class Plan(BaseModel):
    """What the run will do: its workers, and each worker's budget, time and turns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workers: tuple[PlannedWorker, ...] = Field(min_length=1)
    budget_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    worker_seconds: int = Field(gt=0)
    max_turns: int = Field(gt=0)
    result_contract: str | None = Field(default=None, max_length=2000)


def _within(plan: Plan, caps: PlanCaps) -> Plan:
    if len(plan.workers) > caps.max_workers:
        raise PlanRefused(
            f"This fleet can run at most {caps.max_workers} workers; the plan has "
            f"{len(plan.workers)}."
        )
    for worker in plan.workers:
        if worker.model is not None and worker.model not in caps.models:
            raise PlanRefused(f"{worker.model} isn't one of this fleet's models.")
    total = plan.budget_usd * len(plan.workers)
    if total > caps.budget_usd:
        raise PlanRefused(
            f"${total} in total is more than this fleet's ${caps.budget_usd} budget; "
            "an admin or the agent's owner can raise it."
        )
    if plan.worker_seconds > caps.worker_seconds:
        raise PlanRefused(
            f"A worker can run for at most {caps.worker_seconds // 60} min here "
            f"({caps.worker_seconds}s)."
        )
    if plan.max_turns > caps.max_turns:
        raise PlanRefused(f"A worker can take at most {caps.max_turns} turns here.")
    return plan


def build_plan(
    caps: PlanCaps,
    workers: tuple[PlannedWorker, ...],
    *,
    result_contract: str | None = None,
) -> Plan:
    """The recommended defaults: an even share of the budget per worker, the longest
    time and the most turns the caps allow."""
    if not workers:
        raise PlanRefused("A fleet plan needs at least one worker.")
    share = (caps.budget_usd / len(workers)).quantize(_CENT, rounding=ROUND_DOWN)
    if share < _CENT:
        raise PlanRefused(
            f"${caps.budget_usd} can't be shared among {len(workers)} workers; plan fewer."
        )
    return _within(
        Plan(
            workers=workers,
            budget_usd=share,
            worker_seconds=caps.worker_seconds,
            max_turns=caps.max_turns,
            result_contract=result_contract,
        ),
        caps,
    )


def edit_plan(
    plan: Plan,
    caps: PlanCaps,
    *,
    budget_usd: Decimal | None = None,
    worker_seconds: int | None = None,
    max_turns: int | None = None,
    result_contract: str | None = None,
) -> Plan:
    """The person's changes, each kept inside the caps (a refusal names the cap)."""
    update: dict[str, object] = {}
    if budget_usd is not None:
        if budget_usd < _CENT:
            raise PlanRefused("A worker's budget is at least $0.01.")
        update["budget_usd"] = budget_usd.quantize(_CENT, rounding=ROUND_DOWN)
    if worker_seconds is not None:
        if worker_seconds <= 0:
            raise PlanRefused("A worker's time limit must be more than 0.")
        update["worker_seconds"] = worker_seconds
    if max_turns is not None:
        if max_turns <= 0:
            raise PlanRefused("A worker needs at least 1 turn.")
        update["max_turns"] = max_turns
    if result_contract is not None:
        update["result_contract"] = result_contract.strip() or None
    return _within(plan.model_copy(update=update), caps)


def plan_digest(plan: Plan) -> str:
    """What was accepted, pinned: the same plan always hashes the same."""
    payload = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def spawn_within_plan(
    plan: Plan,
    *,
    model: str | None,
    agent: str | None,
    budget_usd: Decimal | None,
    worker_seconds: int | None,
    max_turns: int | None,
) -> tuple[Decimal, int, int]:
    """A spawn's budget, seconds and turns under `plan`: omitted ones take the plan's,
    and none may go past it; its model or agent must be one the plan names."""
    if agent is not None:
        if agent not in {worker.agent for worker in plan.workers}:
            raise PlanRefused(f"The accepted fleet plan doesn't include the agent {agent}.")
    elif model is not None and model not in {worker.model for worker in plan.workers}:
        raise PlanRefused(f"The accepted fleet plan doesn't include {model}.")
    budget = plan.budget_usd if budget_usd is None else budget_usd
    seconds = plan.worker_seconds if worker_seconds is None else worker_seconds
    turns = plan.max_turns if max_turns is None else max_turns
    if budget > plan.budget_usd or seconds > plan.worker_seconds or turns > plan.max_turns:
        raise PlanRefused(
            "That worker goes past the accepted fleet plan (its budget, time or turns). "
            "Ask the person to change the plan instead."
        )
    return budget, seconds, turns
