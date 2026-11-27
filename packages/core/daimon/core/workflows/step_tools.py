"""The in-run tools of an agent step: grant check, contract check and the recorded result.

The step grant is the running attempt's id, which only that step's brief carries; the
caller must also be the run's run-as person in its workspace, and the workflow decider
(`agent_step`) refuses a lane session any other attempt's id. A recorded result is a
`step.output_recorded` ledger event, so it survives the engine; the executor settles the
step from it when the turn ends.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from daimon.core.capability_tools import WORKFLOW_STEP_TOOLS
from daimon.core.stores import workflow_runs
from daimon.core.stores.workflow_run_events import NewEvent, append_events, step_events
from daimon.core.stores.workflow_runs import AttemptRow, RunRow, StepRow
from daimon.core.workflows import engine
from daimon.core.workflows.brief import check_output
from daimon.core.workflows.graph import ancestors
from daimon.core.workflows.runs import account_actor, key_of, node_of, spec_of
from daimon.core.workflows.spec import AgentNode, Spec
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

STEP_TOOLS: Final = WORKFLOW_STEP_TOOLS
MAX_REJECTIONS: Final = 3
BRIEF_SENT: Final = "step.brief_sent"
#: The longest summary a step keeps: a routine's whole final reply fits, as it posted.
SUMMARY_MAX: Final = 40_000
RECORDED: Final = "step.output_recorded"
REJECTED: Final = "step.contract_rejected"
FailKind = Literal["missing_tool", "missing_data", "blocked", "other"]
Result = Literal["complete", "fail", "finish_early"]
_GONE: Final = "This workflow step is not running any more."
_DONE: Final = "Recorded. Stop here: the step is done."


class StepRefused(Exception):
    """A step tool call the agent must change; the message says how."""


@dataclass(frozen=True)
class Grant:
    run: RunRow
    step: StepRow
    attempt: AttemptRow
    spec: Spec
    node: AgentNode

    @property
    def key(self) -> str:
        return key_of(self.step)


@dataclass(frozen=True)
class Recorded:
    result: Result
    output: JsonValue = None
    route: str | None = None
    summary: str | None = None
    reason: str | None = None
    kind: str | None = None


async def grant(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    lock: bool = False,
) -> Grant:
    """The running attempt the caller may act on; `StepRefused` otherwise, the same either way."""
    attempt = await workflow_runs.get_attempt(session, attempt_id)
    step = None if attempt is None else await workflow_runs.get_step(session, attempt.step_id)
    run = None if step is None else await workflow_runs.get_run(session, step.run_id, lock=lock)
    if (
        attempt is None
        or step is None
        or run is None
        or attempt.status != "running"
        or step.status != "running"
        or run.tenant_id != tenant_id
        or run.run_as_account_id != account_id
    ):
        raise StepRefused(_GONE)
    spec = await spec_of(session, run)
    node = node_of(spec, step)
    if not isinstance(node, AgentNode):
        raise StepRefused(_GONE)
    return Grant(run, step, attempt, spec, node)


async def recorded(
    session: AsyncSession, *, run_id: uuid.UUID, key: str, attempt_id: uuid.UUID
) -> Recorded | None:
    """The result an attempt recorded, if any."""
    events = await step_events(session, run_id, step_key=key, types=[RECORDED])
    mine = [e.data for e in events if e.data.get("attempt") == str(attempt_id)]
    if not mine:
        return None
    d = mine[-1]
    return Recorded(
        d["result"],
        d.get("output"),
        d.get("route"),
        d.get("summary"),
        d.get("reason"),
        d.get("kind"),
    )


async def _record(session: AsyncSession, g: Grant, now: datetime, **data: JsonValue) -> None:
    if await recorded(session, run_id=g.run.id, key=g.key, attempt_id=g.attempt.id):
        raise StepRefused("This step already has a result. Stop here.")
    await append_events(
        session,
        run_id=g.run.id,
        events=[NewEvent(RECORDED, g.key, {"attempt": str(g.attempt.id), **data})],
        actor=account_actor(g.run.run_as_account_id or g.run.id),
        at=now,
    )


def _route_errors(node: AgentNode, route: str | None) -> list[str]:
    if not node.routes:
        return [] if route is None else ["This step has no routes: leave route out"]
    if route is not None and route not in node.routes:
        return [f"route must be one of {', '.join(node.routes)}"]
    if route is None and not node.next:
        return [f"Choose a route: {', '.join(node.routes)}"]
    return []


async def complete(
    session: AsyncSession,
    g: Grant,
    *,
    output: JsonValue,
    summary: str,
    route: str | None,
    reason: str | None,
    now: datetime,
) -> str:
    """Record a valid result, or the exact errors (`step.contract_rejected`) for the agent to fix.

    Errors come back as the reply, not a refusal, so the rejection stays on the ledger; the
    fourth rejected try of an attempt is refused outright.
    """
    filled, errors = check_output(g.node.output, output)
    errors += _route_errors(g.node, route)
    if errors:
        events = await step_events(session, g.run.id, step_key=g.key, types=[REJECTED])
        tries = sum(e.data.get("attempt") == str(g.attempt.id) for e in events)
        if tries >= MAX_REJECTIONS:
            raise StepRefused("Too many tries. Call fail_step with what is wrong.")
        await append_events(
            session,
            run_id=g.run.id,
            events=[NewEvent(REJECTED, g.key, {"attempt": str(g.attempt.id), "errors": errors})],
            actor=account_actor(g.run.run_as_account_id or g.run.id),
            at=now,
        )
        return "Not recorded. Fix these and call complete_step again:\n- " + "\n- ".join(errors)
    await _record(
        session,
        g,
        now,
        result="complete",
        output=filled,
        route=route,
        summary=summary,
        reason=reason,
    )
    return _DONE


async def fail(
    session: AsyncSession, g: Grant, *, reason: str, kind: FailKind, now: datetime
) -> str:
    await _record(session, g, now, result="fail", reason=reason, kind=kind)
    return _DONE


async def finish_early(session: AsyncSession, g: Grant, *, summary: str, now: datetime) -> str:
    if not g.node.can_finish_early:
        raise StepRefused("This step cannot finish the run early.")
    await _record(session, g, now, result="finish_early", summary=summary)
    return _DONE


def outcome(r: Recorded) -> engine.Outcome:
    """How the executor settles a recorded result."""
    if r.result == "fail":
        return engine.Failed("agent", r.reason or "The agent failed the step")
    return engine.Succeeded(r.output, r.route, r.summary, early=r.result == "finish_early")


async def context_text(session: AsyncSession, g: Grant) -> str:
    """The brief again, plus the latest outputs of the steps upstream of this one."""
    briefs = await step_events(session, g.run.id, step_key=g.key, types=[BRIEF_SENT])
    brief = next((e.data["brief"] for e in reversed(briefs) if "brief" in e.data), "")
    up = ancestors(g.spec, g.step.node_id)
    latest: dict[str, StepRow] = {}
    for s in await workflow_runs.list_steps(session, g.run.id):
        fresher = s.node_id not in latest or s.visit > latest[s.node_id].visit
        if s.node_id in up and s.status == "succeeded" and fresher:
            latest[s.node_id] = s
    outputs = {
        n: {"output": s.output, "summary": s.summary, "route": s.route} for n, s in latest.items()
    }
    return f"{brief}\n\n<upstream>{json.dumps(outputs, ensure_ascii=False, default=str)}</upstream>"
