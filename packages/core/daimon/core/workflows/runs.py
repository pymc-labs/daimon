"""Runs: the one way a run starts, and every way it moves on afterwards.

Each function is one transaction: it locks the run row (the ledger's seq lock), loads the
steps, asks `engine` for a transition and writes it back with its ledger events, then
NOTIFYs the loops and the run page. `start_run` is the only creator of runs (a contract
test holds that); it and `execute` run `preflight.check` before anything spends.
Executors (agent steps, actions, code; M2 onwards) plug in through `StepExecutor`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal, Protocol

from daimon.core.config import Settings
from daimon.core.stores import security_audit, workflow_runs, workflows
from daimon.core.stores.workflow_run_events import NewEvent, append_events
from daimon.core.stores.workflow_runs import RunRow, StepRow, StepWrite
from daimon.core.workflows import engine
from daimon.core.workflows.engine import Event, Outcome, RunEnd, RunFacts, StepView, Transition
from daimon.core.workflows.graph import ancestors
from daimon.core.workflows.lanes import lane_key
from daimon.core.workflows.preflight import Cleared, Place, Refusal, check
from daimon.core.workflows.spec import (
    ActionNode,
    AgentNode,
    AnyNode,
    Spec,
    parse_duration,
    parse_spec,
)
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]
ENGINE: Final = "engine"
READY_CHANNEL: Final = "workflow_ready"
START_CHANNEL: Final = "workflow_start"
DEFAULT_DEADLINE: Final = timedelta(days=7)
BLOCKED_RECHECK: Final = timedelta(minutes=15)
BLOCKED_FOR: Final = timedelta(days=1)
_TERMINAL: Final = frozenset({"succeeded", "failed", "skipped", "cancelled"})
_MESSAGE_LIMIT: Final = 500


def run_channel(tenant_id: uuid.UUID) -> str:
    """The NOTIFY channel the run page's stream listens on."""
    return f"workflow_run_{tenant_id.hex}"


def account_actor(account_id: uuid.UUID) -> str:
    return f"account:{account_id}"


@dataclass(frozen=True)
class Start:
    """What starts a run. A fork names the run and node to start from; a rerun the run whose
    inputs it reuses. Test runs use the draft, with `pinned` outputs reused for nodes."""

    workflow_id: uuid.UUID
    run_as_account_id: uuid.UUID | None
    triggered_by: dict[str, Any]
    trigger_kind: str = "manual"
    trigger_key: str = "manual"
    inputs: dict[str, Any] | None = None
    mode: Literal["live", "test"] = "live"
    untrusted_inputs: bool = False
    label: str | None = None
    budget_usd: Decimal | None = None
    pinned: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    fork_of: tuple[uuid.UUID, str] | None = None
    rerun_of: uuid.UUID | None = None
    parent_run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class StepWork:
    """One claimed attempt, as an executor gets it."""

    run: RunRow
    step: StepRow
    node: AnyNode
    spec: Spec
    attempt: int
    context: dict[str, JsonValue]
    #: Where the run spends, for an executor that checks preflight again before a nudge.
    place: Place | None = None


class StepExecutor(Protocol):
    async def __call__(self, work: StepWork, /) -> Outcome: ...


@dataclass(frozen=True)
class Loaded:
    run: RunRow
    workflow: workflows.WorkflowRow
    spec: Spec
    steps: list[StepRow]
    facts: RunFacts

    def views(self) -> list[StepView]:
        return [_view(s) for s in self.steps]

    def place(self) -> Place:
        return Place(
            self.run.tenant_id,
            self.workflow,
            self.spec,
            self.run.version,
            self.run.mode,
            self.run.run_as_account_id,
        )


def _view(s: StepRow) -> StepView:
    return StepView(
        s.node_id,
        s.visit,
        s.status,
        route=s.route,
        output=s.output,
        summary=s.summary,
        via=tuple(s.via),
        error_class=s.error_class,
        error_message=s.error_message,
        reused=s.reused,
    )


def _cut(text: str | None) -> str | None:
    return None if text is None else text[:_MESSAGE_LIMIT]


async def _spec_of(session: AsyncSession, run: RunRow) -> Spec:
    if run.mode == "test" and run.test_options is not None:
        return parse_spec(run.test_options["spec"])
    version = await workflows.get_version(
        session, tenant_id=run.tenant_id, workflow_id=run.workflow_id, number=run.version
    )
    if version is None:
        raise LookupError(f"Run {run.id} has no version {run.version}")
    return parse_spec(version.spec)


def _max_steps(spec: Spec, settings: Settings) -> int:
    asked = spec.limits.max_steps if spec.limits and spec.limits.max_steps else None
    return min(asked or engine.DEFAULT_MAX_STEPS, settings.workflows.max_steps)


async def _load(session: AsyncSession, settings: Settings, run: RunRow, now: datetime) -> Loaded:
    workflow = await workflows.get_workflow(
        session, tenant_id=run.tenant_id, workflow_id=run.workflow_id
    )
    assert workflow is not None, "a run's workflow outlives it"
    spec = await _spec_of(session, run)
    pinned = (run.test_options or {}).get("pinned", {})
    facts = RunFacts(
        inputs=run.inputs,
        run={"id": str(run.id), "number": run.number, "mode": run.mode, "label": run.label},
        workflow={"id": str(workflow.id), "name": workflow.name},
        trigger={"kind": run.trigger_kind, "key": run.trigger_key},
        now=now,
        max_steps=_max_steps(spec, settings),
        pinned={
            n: StepView(n, 1, "succeeded", route=p.get("route"), output=p.get("output"))
            for n, p in pinned.items()
        },
    )
    return Loaded(run, workflow, spec, await workflow_runs.list_steps(session, run.id), facts)


async def _lock(session: AsyncSession, run_id: uuid.UUID) -> RunRow | None:
    return await workflow_runs.get_run(session, run_id, lock=True)


def _write(view: StepView, now: datetime) -> StepWrite:
    write: StepWrite = {
        "status": view.status,
        "output": view.output,
        "summary": view.summary,
        "route": view.route,
        "via": list(view.via),
        "error_class": view.error_class,
        "error_message": _cut(view.error_message),
        "reused": view.reused,
        "waiting_reason": "unknown" if view.status == "unknown" else None,
    }
    if view.status == "ready":
        write["ready_at"] = now
    if view.status in _TERMINAL or view.status == "unknown":
        write |= {"lease_owner": None, "lease_expires_at": None}
    if view.status in _TERMINAL:
        write["ended_at"] = now
    return write


async def _apply(
    session: AsyncSession,
    loaded: Loaded,
    transition: Transition,
    *,
    now: datetime,
    actor: str = ENGINE,
    before: Sequence[Event] = (),
    writes: Mapping[tuple[str, int], StepWrite] | None = None,
) -> None:
    """Write a transition: steps, the run's projected status, ledger events, audit, NOTIFY."""
    run = loaded.run
    changed = {(v.node_id, v.visit): _write(v, now) for v in transition.steps}
    for (node_id, visit), write in changed.items():
        if isinstance(node := loaded.spec.nodes.get(node_id), AgentNode):
            write["lane"] = lane_key(loaded.spec, node_id, node, visit)
    changed |= writes or {}
    stale = {k: w for k, w in changed.items() if w.get("status") == "cancelled"}
    for batch, bump in (
        (stale, True),
        ({k: w for k, w in changed.items() if k not in stale}, False),
    ):
        await workflow_runs.save_steps(
            session, tenant_id=run.tenant_id, run_id=run.id, steps=batch, bump_generation=bump
        )
    rows = await workflow_runs.list_steps(session, run.id)
    for r in rows:
        if (r.node_id, r.visit) in stale:
            # A cancelled attempt is over: a later claim never reattaches to it.
            await workflow_runs.finish_attempt(session, step_id=r.id, status="abandoned", now=now)
    events = list(before) + list(transition.events)
    end = transition.end
    if end is not None:
        await workflow_runs.update_run(
            session,
            run.id,
            status=end.status,
            waiting_reason=None,
            outcome=end.outcome,
            failure_reason=end.reason if end.status != "succeeded" else None,
            failure_message=_cut(end.message) if end.status != "succeeded" else None,
            failure_step=end.step if end.status != "succeeded" else None,
            ended_at=now,
        )
        await workflow_runs.close_waits(session, status="cancelled", now=now, run_id=run.id)
    else:
        projected = engine.project_status([_view(r) for r in rows])
        status, reason = projected or ("running", None)
        if status == "waiting" and reason is None:
            reason = next((r.waiting_reason for r in rows if r.status == "waiting"), "unknown")
        if (status, reason) != (run.status, run.waiting_reason):
            if status == "waiting":
                events.append(Event("run.waiting", None, {"reason": reason}))
            elif run.status == "waiting":
                events.append(Event("run.resumed", None, {}))
            await workflow_runs.update_run(session, run.id, status=status, waiting_reason=reason)
    head = await append_events(
        session,
        run_id=run.id,
        events=[NewEvent(e.type, e.step, dict(e.data)) for e in events],
        actor=actor,
        at=now,
    )
    if end is not None:
        seq = await workflow_runs.get_run(session, run.id)
        await security_audit.append_event(
            session,
            tenant_id=run.tenant_id,
            account_id=run.run_as_account_id,
            agent_id=None,
            platform=None,
            platform_user_id=None,
            tool_name="workflow_run",
            operation=end.status,
            outcome="allowed",
            reason=f"run {run.id} seq {seq.last_seq if seq else 0} head {head}",
            occurred_at=now,
        )
    if any(r.status == "ready" for r in rows):
        await _notify(session, READY_CHANNEL, str(run.id))
    await _notify(session, run_channel(run.tenant_id), str(run.id))


async def _notify(session: AsyncSession, channel: str, payload: str) -> None:
    await session.execute(select(func.pg_notify(channel, payload)))


# --- starting ---


def _deadline(spec: Spec, settings: Settings, now: datetime) -> datetime:
    asked = parse_duration(spec.limits.deadline) if spec.limits and spec.limits.deadline else None
    return now + min(
        asked or DEFAULT_DEADLINE, timedelta(days=settings.workflows.max_deadline_days)
    )


def _budget(spec: Spec, settings: Settings, start: Start, cleared: Cleared) -> Decimal:
    limits = spec.limits.budget_usd if spec.limits and spec.limits.budget_usd else None
    asked = start.budget_usd or (Decimal(str(limits)) if limits else cleared.run_budget_usd)
    return min(asked, settings.workflows.max_run_budget_usd)


def _active_limit(spec: Spec, settings: Settings) -> int:
    asked = spec.concurrency.limit if spec.concurrency and spec.concurrency.limit else None
    return min(asked or settings.workflows.max_active_runs, settings.workflows.max_active_runs)


async def start_run(
    sm: Sessions,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    start: Start,
    now: datetime | None = None,
) -> RunRow | Refusal:
    """The single entry for every run: manual, trigger, test, fork, rerun, child.

    Preflight runs first and a refusal creates nothing. The run begins at once unless the
    workflow already has its limit of active runs; the starts loop promotes it later.
    """
    now = now or datetime.now(UTC)
    async with sm() as session:
        workflow = await workflows.get_workflow(
            session, tenant_id=tenant_id, workflow_id=start.workflow_id
        )
        if workflow is None:
            return Refusal("not_found", "This workflow is gone.")
        source = None
        if start.fork_of or start.rerun_of:
            source_id = start.fork_of[0] if start.fork_of else start.rerun_of
            assert source_id is not None
            source = await workflow_runs.get_run(session, source_id, tenant_id=tenant_id)
            if source is None or source.workflow_id != workflow.id:
                return Refusal("not_found", "That run is gone.")
        if start.mode == "test":
            spec_document: dict[str, Any] = workflow.draft_spec
            version = workflow.live_version or 0
        elif workflow.status != "active":
            return Refusal("paused", "This workflow is paused. Ask its owner.")
        elif workflow.live_version is None:
            return Refusal("not_published", "Publish this workflow before running it.")
        else:
            version = source.version if source and start.fork_of else workflow.live_version
            row = await workflows.get_version(
                session, tenant_id=tenant_id, workflow_id=workflow.id, number=version
            )
            assert row is not None, "the live version exists"
            spec_document = row.spec
        source_steps = await workflow_runs.list_steps(session, source.id) if source else []
    spec = parse_spec(spec_document)
    given = start.inputs if start.inputs is not None else (source.inputs if source else {})
    inputs, problems = engine.resolve_inputs(spec, given)
    if problems:
        return Refusal("invalid_inputs", problems[0])
    if start.fork_of and start.fork_of[1] not in spec.nodes:
        return Refusal("not_found", "That step is not in this version.")
    place = Place(tenant_id, workflow, spec, version, start.mode, start.run_as_account_id)
    cleared = await check(sm, settings, place, now=now)
    if isinstance(cleared, Refusal):
        return cleared
    async with sm.begin() as session:
        run = await workflow_runs.create_run(
            session,
            tenant_id=tenant_id,
            workflow_id=workflow.id,
            version=version,
            mode=start.mode,
            run_as_account_id=start.run_as_account_id,
            triggered_by=start.triggered_by,
            trigger_kind=start.trigger_kind,
            trigger_key=start.trigger_key,
            inputs=inputs,
            untrusted_inputs=start.untrusted_inputs or bool(source and source.untrusted_inputs),
            label=start.label,
            budget_usd=_budget(spec, settings, start, cleared),
            deadline_at=_deadline(spec, settings, now),
            parent_run_id=start.parent_run_id,
            forked_from_run_id=start.fork_of[0] if start.fork_of else None,
            forked_from_step=start.fork_of[1] if start.fork_of else None,
            test_options=(
                {"spec": spec_document, "pinned": start.pinned} if start.mode == "test" else None
            ),
        )
        queued = [Event("run.queued", None, {"trigger": start.trigger_kind})]
        if start.fork_of:
            copied = _fork_copies(spec, source_steps, start.fork_of[1])
            await workflow_runs.save_steps(
                session, tenant_id=tenant_id, run_id=run.id, steps=copied
            )
            queued.append(
                Event(
                    "run.forked_from",
                    None,
                    {"run": str(start.fork_of[0]), "node": start.fork_of[1]},
                )
            )
        await append_events(
            session,
            run_id=run.id,
            events=[NewEvent(e.type, e.step, dict(e.data)) for e in queued],
            actor=ENGINE,
            at=now,
        )
        active = await workflow_runs.count_active_runs(session, workflow_id=workflow.id)
        if active < _active_limit(spec, settings):
            await _begin(session, settings, run, now)
        else:
            await _notify(session, START_CHANNEL, str(run.id))
        result = await workflow_runs.get_run(session, run.id)
    assert result is not None
    return result


def _fork_copies(
    spec: Spec, steps: Sequence[StepRow], node_id: str
) -> dict[tuple[str, int], StepWrite]:
    """The latest succeeded visit of every node before `node_id`, marked reused."""
    before = ancestors(spec, node_id) - {node_id}
    latest: dict[str, StepRow] = {}
    for s in steps:
        newer = s.node_id not in latest or s.visit > latest[s.node_id].visit
        if s.node_id in before and s.status == "succeeded" and newer:
            latest[s.node_id] = s
    return {
        (s.node_id, s.visit): {
            "status": "succeeded",
            "output": s.output,
            "summary": s.summary,
            "route": s.route,
            "via": s.via,
            "reused": "fork",
        }
        for s in latest.values()
    }


async def _begin(session: AsyncSession, settings: Settings, run: RunRow, now: datetime) -> None:
    await workflow_runs.update_run(session, run.id, status="running", started_at=now)
    run = run.model_copy(update={"status": "running"})
    loaded = await _load(session, settings, run, now)
    if run.forked_from_step:
        transition = engine.enter_at(
            loaded.spec, loaded.views(), run.forked_from_step, (), loaded.facts
        )
    else:
        transition = engine.begin(loaded.spec, loaded.facts)
    await _apply(session, loaded, transition, now=now, before=[Event("run.started", None, {})])


async def promote_queued(
    sm: Sessions, settings: Settings, run_id: uuid.UUID, *, now: datetime
) -> bool:
    """Begin a queued run if its workflow has room now."""
    async with sm.begin() as session:
        run = await workflow_runs.get_run(session, run_id, lock=True, skip_locked=True)
        if run is None or run.status != "queued":
            return False
        await workflows.get_workflow(
            session, tenant_id=run.tenant_id, workflow_id=run.workflow_id, lock=True
        )
        spec = await _spec_of(session, run)
        active = await workflow_runs.count_active_runs(session, workflow_id=run.workflow_id)
        if active >= _active_limit(spec, settings):
            return False
        await _begin(session, settings, run, now)
        return True


# --- executing steps ---


@dataclass(frozen=True)
class Claimed:
    run: RunRow
    step: StepRow
    attempt: int


async def claim(
    sm: Sessions, *, step_id: uuid.UUID, owner: str, lease: timedelta, now: datetime
) -> Claimed | None:
    async with sm.begin() as session:
        got = await workflow_runs.claim_step(
            session, step_id=step_id, owner=owner, lease_until=now + lease, now=now
        )
        if got is None:
            return None
        run, step, attempt = got
        await append_events(
            session,
            run_id=run.id,
            events=[NewEvent("step.started", _key(step), {"attempt": attempt})],
            actor=ENGINE,
            at=now,
        )
        await _notify(session, run_channel(run.tenant_id), str(run.id))
    return Claimed(run, step, attempt)


def _key(step: StepRow) -> str:
    return engine.step_key(step.node_id, step.visit)


async def execute(
    sm: Sessions,
    settings: Settings,
    executors: Mapping[str, StepExecutor],
    claimed: Claimed,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Run one claimed attempt: preflight, then the node kind's executor, then settle."""
    async with sm() as session:
        loaded = await _load(session, settings, claimed.run, clock())
    node = loaded.spec.nodes[claimed.step.node_id]
    refusal = await check(
        sm,
        settings,
        loaded.place(),
        now=clock(),
        budget_usd=loaded.run.budget_usd,
        cost_usd=loaded.run.cost_usd,
    )
    outcome: Outcome
    if isinstance(refusal, Refusal):
        if refusal.gate:
            await _block(sm, settings, claimed, refusal, now=clock())
            return
        outcome = engine.Failed(
            "fatal" if refusal.reason != "over_budget" else "gate",
            refusal.message,
            reason=refusal.reason,
        )
    elif (executor := executors.get(node.kind)) is None:
        outcome = engine.Failed("fatal", f"{node.kind.capitalize()} steps are not available yet")
    else:
        work = StepWork(
            loaded.run,
            claimed.step,
            node,
            loaded.spec,
            claimed.attempt,
            engine.expression_context(
                loaded.spec, loaded.views(), node_id=claimed.step.node_id, facts=loaded.facts
            ),
            loaded.place(),
        )
        try:
            outcome = await executor(work)
        except Exception as err:  # an executor bug fails its step, never the loop
            outcome = engine.Failed("fatal", _cut(f"{type(err).__name__}: {err}") or "")
    await settle(
        sm,
        settings,
        step_id=claimed.step.id,
        generation=claimed.step.generation,
        outcome=outcome,
        now=clock(),
    )


async def _locked_step(
    session: AsyncSession, step_id: uuid.UUID, generation: int | None, status: str
) -> tuple[RunRow, StepRow] | None:
    """Run then step, locked; None when the step moved on from `status` or `generation`."""
    run_id = await workflow_runs.run_of_step(session, step_id)
    run = None if run_id is None else await _lock(session, run_id)
    step = None if run is None else await workflow_runs.lock_step(session, step_id)
    if run is None or step is None or step.status != status:
        return None
    if generation is not None and step.generation != generation:
        return None
    return run, step


async def settle(
    sm: Sessions,
    settings: Settings,
    *,
    step_id: uuid.UUID,
    generation: int,
    outcome: Outcome,
    now: datetime,
) -> bool:
    """Record an attempt's outcome; False when the step was cancelled or reclaimed meanwhile."""
    async with sm.begin() as session:
        held = await _locked_step(session, step_id, generation, "running")
        if held is None:
            return False
        run, step = held
        attempts = await workflow_runs.list_attempts(session, step_id)
        number = attempts[-1].number if attempts else 1
        failed = isinstance(outcome, engine.Failed)
        await workflow_runs.finish_attempt(
            session,
            step_id=step_id,
            status="abandoned"
            if isinstance(outcome, engine.Unknown)
            else "failed"
            if failed
            else "succeeded",
            now=now,
            error_class=outcome.error_class if isinstance(outcome, engine.Failed) else None,
            error_message=_cut(outcome.message) if isinstance(outcome, engine.Failed) else None,
        )
        loaded = await _load(session, settings, run, now)
        node = loaded.spec.nodes[step.node_id]
        delay = engine.retry_delay_s(node, number, outcome)
        if delay is not None and isinstance(outcome, engine.Failed):
            await _retry_later(
                session,
                loaded,
                step,
                now=now,
                delay=timedelta(seconds=delay),
                message=outcome.message,
            )
            return True
        transition = engine.resolve(
            loaded.spec, loaded.views(), step.node_id, step.visit, outcome, loaded.facts
        )
        await _apply(session, loaded, transition, now=now)
        return True


async def _retry_later(
    session: AsyncSession,
    loaded: Loaded,
    step: StepRow,
    *,
    now: datetime,
    delay: timedelta,
    message: str,
) -> None:
    write: StepWrite = {
        "status": "ready",
        "ready_at": now + delay,
        "lease_owner": None,
        "lease_expires_at": None,
        "error_message": _cut(message),
    }
    event = Event(
        "step.retry_scheduled",
        _key(step),
        {"after_s": int(delay.total_seconds()), "message": _cut(message)},
    )
    await _apply(
        session,
        loaded,
        Transition((), (event,), None),
        now=now,
        writes={(step.node_id, step.visit): write},
    )


async def _block(
    sm: Sessions, settings: Settings, claimed: Claimed, refusal: Refusal, *, now: datetime
) -> None:
    """A spend gate refused the step: it waits `blocked` and the waits loop rechecks it."""
    async with sm.begin() as session:
        held = await _locked_step(session, claimed.step.id, claimed.step.generation, "running")
        if held is None:
            return
        run, step = held
        await workflow_runs.finish_attempt(
            session,
            step_id=step.id,
            status="abandoned",
            now=now,
            error_class="gate",
            error_message=refusal.message,
        )
        wait = await workflow_runs.open_wait(
            session,
            tenant_id=run.tenant_id,
            run_id=run.id,
            step_id=step.id,
            kind="blocked",
            expires_at=now + BLOCKED_FOR,
            remind_at=now + BLOCKED_RECHECK,
            shown={"reason": refusal.reason, "message": refusal.message},
            now=now,
        )
        write: StepWrite = {
            "status": "waiting",
            "waiting_reason": "blocked",
            "lease_owner": None,
            "lease_expires_at": None,
        }
        event = Event(
            "wait.opened",
            _key(step),
            {"wait": str(wait.id), "kind": "blocked", "reason": refusal.reason},
        )
        await _apply(
            session,
            await _load(session, settings, run, now),
            Transition((), (event,), None),
            now=now,
            writes={(step.node_id, step.visit): write},
        )


async def recheck_blocked(
    sm: Sessions, settings: Settings, wait: workflow_runs.WaitRow, *, now: datetime
) -> None:
    """A blocked step's recheck: ready again once the gate clears, failed when the day is up."""
    async with sm() as session:
        run = await workflow_runs.get_run(session, wait.run_id)
        loaded = None if run is None else await _load(session, settings, run, now)
    if loaded is None:
        return
    expired = wait.expires_at is not None and wait.expires_at <= now
    refusal = (
        None
        if expired
        else await check(
            sm,
            settings,
            loaded.place(),
            now=now,
            budget_usd=loaded.run.budget_usd,
            cost_usd=loaded.run.cost_usd,
        )
    )
    async with sm.begin() as session:
        held = await _locked_step(session, wait.step_id, None, "waiting")
        if held is None:
            return
        run, step = held
        if isinstance(refusal, Refusal) and refusal.gate:
            await workflow_runs.set_remind_at(session, wait.id, now + BLOCKED_RECHECK)
            return
        loaded = await _load(session, settings, run, now)
        if expired or isinstance(refusal, Refusal):
            await workflow_runs.close_waits(session, status="timed_out", now=now, step_id=step.id)
            shown = wait.shown or {}
            reason = (
                refusal.reason
                if isinstance(refusal, Refusal)
                else str(shown.get("reason", "step_failed"))
            )
            message = (
                refusal.message if isinstance(refusal, Refusal) else str(shown.get("message", ""))
            )
            transition = engine.resolve(
                loaded.spec,
                loaded.views(),
                step.node_id,
                step.visit,
                engine.Failed("gate", message, reason=reason),
                loaded.facts,
            )
            event = Event("wait.timed_out", _key(step), {"wait": str(wait.id)})
            await _apply(session, loaded, transition, now=now, before=[event])
            return
        await workflow_runs.close_waits(session, status="completed", now=now, step_id=step.id)
        event = Event("wait.decided", _key(step), {"wait": str(wait.id), "decision": "cleared"})
        write: StepWrite = {"status": "ready", "ready_at": now, "waiting_reason": None}
        await _apply(
            session,
            loaded,
            Transition((), (event,), None),
            now=now,
            writes={(step.node_id, step.visit): write},
        )


async def reclaim(sm: Sessions, settings: Settings, *, step_id: uuid.UUID, now: datetime) -> None:
    """A lease ran out with its worker gone. An agent step whose session was saved keeps its
    attempt and is reattached to that session. Nothing sent yet, or an idempotent node: run
    it again. Otherwise a write may have happened, and the step is `unknown` for its owner."""
    async with sm.begin() as session:
        held = await _locked_step(session, step_id, None, "running")
        if held is None or (held[1].lease_expires_at and held[1].lease_expires_at > now):
            return
        run, step = held
        attempts = await workflow_runs.list_attempts(session, step_id)
        loaded = await _load(session, settings, run, now)
        node = loaded.spec.nodes[step.node_id]
        if isinstance(node, AgentNode) and attempts and attempts[-1].session_id is not None:
            await _retry_later(
                session, loaded, step, now=now, delay=timedelta(0), message="Reattaching"
            )
            return
        await workflow_runs.finish_attempt(
            session, step_id=step_id, status="abandoned", now=now, error_class="unknown"
        )
        sent = bool(attempts) and attempts[-1].send_intent_at is not None
        if not sent or (isinstance(node, ActionNode) and node.idempotent):
            await _retry_later(
                session, loaded, step, now=now, delay=timedelta(0), message="The worker stopped"
            )
            return
        outcome = engine.Unknown("The worker stopped after it may have written something")
        transition = engine.resolve(
            loaded.spec, loaded.views(), step.node_id, step.visit, outcome, loaded.facts
        )
        await _apply(session, loaded, transition, now=now)


async def stop_run(
    sm: Sessions,
    settings: Settings,
    *,
    run_id: uuid.UUID,
    end: RunEnd,
    now: datetime,
    actor: str = ENGINE,
) -> bool:
    """End an active run from outside (cancel, deadline, budget); running settles go stale."""
    async with sm.begin() as session:
        run = await _lock(session, run_id)
        if run is None or run.status not in workflow_runs.ACTIVE_RUN_STATES:
            return False
        loaded = await _load(session, settings, run, now)
        await _apply(
            session,
            loaded,
            engine.stop(loaded.views(), loaded.spec, loaded.facts, end),
            now=now,
            actor=actor,
        )
        return True


async def cancel_run(
    sm: Sessions, settings: Settings, *, run_id: uuid.UUID, account_id: uuid.UUID, now: datetime
) -> bool:
    end = RunEnd("cancelled", reason="cancelled_by", message=f"Cancelled by {account_id}")
    return await stop_run(
        sm, settings, run_id=run_id, end=end, now=now, actor=account_actor(account_id)
    )


async def redrive(
    sm: Sessions,
    settings: Settings,
    *,
    run_id: uuid.UUID,
    account_id: uuid.UUID,
    now: datetime,
    budget_usd: Decimal | None = None,
    deadline_at: datetime | None = None,
) -> RunRow | Refusal:
    """Retry from failure: the same run and version re-runs its failed and cancelled steps."""
    async with sm() as session:
        run = await workflow_runs.get_run(session, run_id)
        loaded = None if run is None else await _load(session, settings, run, now)
    if loaded is None or loaded.run.status not in ("failed", "timed_out"):
        return Refusal("not_redrivable", "Only a failed or timed out run can be retried.")
    budget = min(budget_usd or loaded.run.budget_usd, settings.workflows.max_run_budget_usd)
    deadline = deadline_at or loaded.run.deadline_at
    if deadline is not None and deadline <= now:
        return Refusal("deadline", "The run's deadline has passed. Give it a later one.")
    cleared = await check(
        sm, settings, loaded.place(), now=now, budget_usd=budget, cost_usd=loaded.run.cost_usd
    )
    if isinstance(cleared, Refusal):
        return cleared
    async with sm.begin() as session:
        run = await _lock(session, run_id)
        if run is None or run.status not in ("failed", "timed_out"):
            return Refusal("not_redrivable", "Only a failed or timed out run can be retried.")
        loaded = await _load(session, settings, run, now)
        transition = engine.redrive(loaded.spec, loaded.views(), loaded.facts)
        if transition is None:
            return Refusal("not_redrivable", "Nothing failed in this run.")
        count = run.redrive_count + 1
        await workflow_runs.update_run(
            session,
            run.id,
            status="running",
            outcome=None,
            failure_reason=None,
            failure_message=None,
            failure_step=None,
            ended_at=None,
            redrive_count=count,
            budget_usd=budget,
            deadline_at=deadline,
        )
        loaded = _running(loaded)
        event = Event("run.redriven", None, {"count": count})
        await _apply(
            session, loaded, transition, now=now, actor=account_actor(account_id), before=[event]
        )
        result = await workflow_runs.get_run(session, run.id)
    assert result is not None
    return result


def _running(loaded: Loaded) -> Loaded:
    run = loaded.run.model_copy(update={"status": "running", "waiting_reason": None})
    return Loaded(run, loaded.workflow, loaded.spec, loaded.steps, loaded.facts)


async def decide_unknown(
    sm: Sessions,
    settings: Settings,
    *,
    step_id: uuid.UUID,
    decision: Literal["retry", "skip", "fail"],
    account_id: uuid.UUID,
    now: datetime,
    output: JsonValue = None,
) -> bool:
    """The owner's choice for an `unknown` step: run it again, skip it with an output, or fail."""
    async with sm.begin() as session:
        held = await _locked_step(session, step_id, None, "unknown")
        if held is None:
            return False
        run, step = held
        loaded = await _load(session, settings, run, now)
        args = (loaded.spec, loaded.views(), step.node_id, step.visit)
        match decision:
            case "retry":
                transition = engine.retry_step(*args, loaded.facts)
            case "skip":
                transition = engine.resolve(*args, engine.Succeeded(output), loaded.facts)
            case "fail":
                failed = engine.Failed("unknown", "Marked failed by its owner")
                transition = engine.resolve(*args, failed, loaded.facts)
        event = Event(
            "step.resolved_by", _key(step), {"decision": decision, "account": str(account_id)}
        )
        await _apply(
            session, loaded, transition, now=now, actor=account_actor(account_id), before=[event]
        )
        return True


# --- the starts and sweep loops ---


async def start_from_trigger_events(
    sm: Sessions, settings: Settings, *, now: datetime, limit: int = 20
) -> int:
    """Turn accepted trigger events into runs, as the live version's publisher."""
    started = 0
    async with sm.begin() as session:
        for event, workflow_id, kind, key in await workflow_runs.claim_trigger_events(
            session, limit=limit
        ):
            workflow = await workflows.get_workflow(
                session, tenant_id=event.tenant_id, workflow_id=workflow_id
            )
            live = None
            if workflow is not None and workflow.live_version is not None:
                live = await workflows.get_version(
                    session,
                    tenant_id=event.tenant_id,
                    workflow_id=workflow_id,
                    number=workflow.live_version,
                )
            result: RunRow | Refusal = Refusal("not_published", "No live version")
            if live is not None:
                result = await start_run(
                    sm,
                    settings,
                    tenant_id=event.tenant_id,
                    start=Start(
                        workflow_id=workflow_id,
                        run_as_account_id=live.published_by_account_id,
                        triggered_by={
                            "kind": kind,
                            "trigger_id": str(event.trigger_id),
                            "event_id": str(event.id),
                        },
                        trigger_kind=kind,
                        trigger_key=key,
                        inputs=event.inputs or {},
                        untrusted_inputs=kind in ("webhook", "event"),
                    ),
                    now=now,
                )
            if isinstance(result, Refusal):
                await workflow_runs.mark_trigger_event(
                    session, event.id, status="rejected", reason=result.reason
                )
            else:
                started += 1
                await workflow_runs.mark_trigger_event(
                    session, event.id, status="started", run_id=result.id
                )
    return started


async def sweep(
    sm: Sessions,
    settings: Settings,
    *,
    now: datetime,
    retention_days: Callable[[uuid.UUID], Awaitable[int]],
) -> None:
    """Clear old payloads and delete finished runs past their workspace's retention.

    `retention_days(tenant_id)` awaits to the workspace's retention in days.
    """
    async with sm.begin() as session:
        before = now - timedelta(days=settings.workflows.payload_retention_days)
        await workflow_runs.clear_payloads(session, before=before)
        tenant_ids = await workflow_runs.tenants_with_finished_runs(session)
    for tenant_id in tenant_ids:
        days = await retention_days(tenant_id)
        async with sm.begin() as session:
            old = await workflow_runs.finished_runs_before(
                session, tenant_id=tenant_id, before=now - timedelta(days=days), limit=500
            )
            await workflow_runs.delete_runs(session, old)
