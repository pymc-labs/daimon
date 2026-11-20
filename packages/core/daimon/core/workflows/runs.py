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
from daimon.core.stores import (
    security_audit,
    workflow_runs,
    workflow_triggers,
    workflow_waits,
    workflows,
)
from daimon.core.stores.workflow_run_events import NewEvent, append_events
from daimon.core.stores.workflow_runs import RunRow, StepRow, StepWrite
from daimon.core.workflows import asks, callbacks, delivery, engine
from daimon.core.workflows.asks import Ask
from daimon.core.workflows.engine import Event, Outcome, RunEnd, RunFacts, StepView, Transition
from daimon.core.workflows.expressions import ExpressionError, evaluate, render
from daimon.core.workflows.graph import ancestors
from daimon.core.workflows.items import ItemState, item_label, items_next, items_problem
from daimon.core.workflows.lanes import lane_key
from daimon.core.workflows.preflight import Cleared, Place, Refusal, check
from daimon.core.workflows.spec import (
    ActionNode,
    AgentNode,
    AnyNode,
    CodeNode,
    FinishNode,
    ForEachNode,
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
WAITS_CHANNEL: Final = "workflow_waits"
#: How long an event that no wait took yet is held for one that opens later.
EVENT_BUFFER: Final = timedelta(hours=1)
DEFAULT_DEADLINE: Final = timedelta(days=7)
BLOCKED_RECHECK: Final = timedelta(minutes=15)
BLOCKED_FOR: Final = timedelta(days=1)
_TERMINAL: Final = frozenset({"succeeded", "failed", "skipped", "cancelled"})
_MESSAGE_LIMIT: Final = 500
#: Runs a key may queue behind its active ones.
QUEUE_LIMIT: Final = 100
#: How many runs deep a chain of event or call starts may go.
MAX_CHAIN_DEPTH: Final = 3


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
    #: The run the pinned outputs came from, for the run page.
    pinned_from: uuid.UUID | None = None
    fork_of: tuple[uuid.UUID, str] | None = None
    rerun_of: uuid.UUID | None = None
    parent_run_id: uuid.UUID | None = None
    #: The Workflow step that waits on this run, which settles from its result.
    parent_step_id: uuid.UUID | None = None
    #: A published version to run instead of the live one.
    version: int | None = None


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


@dataclass(frozen=True)
class Park:
    """The step waits instead of settling: on the open wait `wait` (a question its tool
    opened), or on a new wait of `kind` that `execute` opens with the step."""

    kind: Literal["approval", "question", "timer", "event", "blocked"]
    wait: uuid.UUID | None = None
    addressees: tuple[str, ...] = ()
    deliver: str | None = None
    min_approvals: int = 1
    shown: dict[str, Any] | None = None
    event_source: str | None = None
    event_key: str | None = None
    expires_at: datetime | None = None
    remind_at: datetime | None = None
    #: What the wait posts on chat (an approval's DMs or thread).
    ask: Ask | None = None


@dataclass(frozen=True)
class Parked:
    """An executor's answer for a step that waits on a child run it started."""

    child_run_id: uuid.UUID
    number: int


class StepExecutor(Protocol):
    async def __call__(self, work: StepWork, /) -> Outcome | Park | Parked: ...


def node_of(spec: Spec, step: StepRow) -> AnyNode:
    """What a step runs: its node, or a For each item's inline node."""
    node = spec.nodes[step.node_id]
    return node.do if isinstance(node, ForEachNode) and step.item_index is not None else node


def key_of(step: StepRow) -> str:
    """The step's ledger key: `node`, `node#2`, or `node[3]` for an item."""
    if step.item_index is None:
        return engine.step_key(step.node_id, step.visit)
    return engine.item_key(step.node_id, step.visit, step.item_index)


def _item_of(step: StepRow) -> JsonValue:
    inputs: dict[str, JsonValue] = step.inputs or {}
    return inputs.get("item")


def item_context(spec: Spec, step: StepRow) -> dict[str, JsonValue]:
    """An item's own names: `item`, `index`, and the For each's `as` name for the item."""
    node = spec.nodes[step.node_id]
    if step.item_index is None or not isinstance(node, ForEachNode):
        return {}
    item = _item_of(step)
    names: dict[str, JsonValue] = {"item": item, "index": step.item_index}
    return names | ({node.as_: item} if node.as_ else {})


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
        files=tuple(s.files),
        error_class=s.error_class,
        error_message=s.error_message,
        reused=s.reused,
    )


def _cut(text: str | None) -> str | None:
    return None if text is None else text[:_MESSAGE_LIMIT]


async def spec_of(session: AsyncSession, run: RunRow) -> Spec:
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
    spec = await spec_of(session, run)
    pinned = (run.test_options or {}).get("pinned", {})
    facts = RunFacts(
        inputs=run.inputs,
        run={
            "id": str(run.id),
            "number": run.number,
            "mode": run.mode,
            "label": run.label,
            "callbacks": dict(callbacks.urls(settings, spec, run.id)),
        },
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
        "files": list(view.files),
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
    events = list(before) + list(transition.events)
    for r in rows:
        if (r.node_id, r.visit) in stale:
            # A cancelled attempt is over: a later claim never reattaches to it.
            await workflow_runs.finish_attempt(session, step_id=r.id, status="abandoned", now=now)
            if isinstance(loaded.spec.nodes.get(r.node_id), ForEachNode):
                events += await _cancel_items(session, r, now=now, reason="parent_cancelled")
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
        events += await _queue_posts(session, loaded, end, [_view(r) for r in rows])
        if run.mode == "live":
            await _offer_finished(session, loaded, rows, end, now)
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
    for r in rows:
        if r.status == "ready" and isinstance(loaded.spec.nodes.get(r.node_id), ForEachNode):
            await _fan_out(session, loaded, r.id, now=now)


# --- For each items ---


async def _reload(session: AsyncSession, loaded: Loaded) -> Loaded:
    run = await workflow_runs.get_run(session, loaded.run.id)
    assert run is not None, "a locked run stays"
    steps = await workflow_runs.list_steps(session, run.id)
    return Loaded(run, loaded.workflow, loaded.spec, steps, loaded.facts)


def _ended(status: str, now: datetime) -> StepWrite:
    return {"status": status, "lease_owner": None, "lease_expires_at": None, "ended_at": now}


async def _cancel_items(
    session: AsyncSession, parent: StepRow, *, now: datetime, reason: str
) -> list[Event]:
    items = await workflow_runs.list_items(session, run_id=parent.run_id, parent_step_id=parent.id)
    going = [i for i in items if i.status in engine.ACTIVE]
    if not going:
        return []
    await workflow_runs.save_items(
        session,
        parent=parent,
        items={i.item_index or 0: _ended("cancelled", now) for i in going},
        bump_generation=True,
    )
    for i in going:
        await workflow_runs.finish_attempt(session, step_id=i.id, status="abandoned", now=now)
    return [Event("step.cancelled", key_of(i), {"reason": reason}) for i in going]


async def _fan_out(
    session: AsyncSession, loaded: Loaded, step_id: uuid.UUID, *, now: datetime
) -> None:
    """A ready For each step starts: its items become rows, the first `concurrency` ready.

    Run again (redrive), items that succeeded keep their outputs and only the rest re-run.
    """
    parent = await workflow_runs.get_step(session, step_id)
    if parent is None or parent.status != "ready":
        return  # an earlier fan-out in this transaction already settled it
    loaded = await _reload(session, loaded)
    node = loaded.spec.nodes[parent.node_id]
    assert isinstance(node, ForEachNode)
    key = key_of(parent)
    context = engine.expression_context(loaded.spec, loaded.views(), parent.node_id, loaded.facts)
    try:
        value: JsonValue = render(node.items, context, now=now)
        problem = items_problem(value)
    except ExpressionError as err:
        value, problem = None, str(err)
    await workflow_runs.save_steps(
        session,
        tenant_id=parent.tenant_id,
        run_id=parent.run_id,
        steps={(parent.node_id, parent.visit): {"status": "running", "started_at": now}},
    )
    total = len(value) if isinstance(value, list) else 0
    attempt = loaded.run.redrive_count + 1
    started = Event("step.started", key, {"attempt": attempt, "total": total})
    if problem is not None or not isinstance(value, list):
        transition = engine.resolve(
            loaded.spec,
            loaded.views(),
            parent.node_id,
            parent.visit,
            engine.Failed("fatal", problem or "The items are not a list"),
            loaded.facts,
        )
        await _apply(session, loaded, transition, now=now, before=[started])
        return
    kept = {
        i.item_index
        for i in await workflow_runs.list_items(
            session, run_id=parent.run_id, parent_step_id=parent.id
        )
        if i.status == "succeeded"
    }
    again: dict[int, StepWrite] = {
        i: {
            "status": "pending",
            "output": None,
            "error_class": None,
            "error_message": None,
            "ended_at": None,
            "lane": _item_lane(loaded.spec, node, parent, i),
        }
        for i in range(total)
        if i not in kept
    }
    await workflow_runs.save_items(
        session,
        parent=parent,
        items=again,
        inputs={i: {"item": value[i], "index": i} for i in again},
        bump_generation=True,
    )
    await _advance_items(session, await _reload(session, loaded), parent, now=now, before=[started])


def _item_lane(spec: Spec, node: ForEachNode, parent: StepRow, index: int) -> str | None:
    """Each agent item is a lane of its own (the `@` form a busy lane takes)."""
    if not isinstance(node.do, AgentNode):
        return None
    lane = lane_key(spec, parent.node_id, node.do, parent.visit)
    return f"{lane}@{engine.item_key(parent.node_id, parent.visit, index)}"


async def _advance_items(
    session: AsyncSession,
    loaded: Loaded,
    parent: StepRow,
    *,
    now: datetime,
    before: Sequence[Event] = (),
) -> None:
    """Start the items there is room for, or settle the For each once its items are done."""
    node = loaded.spec.nodes[parent.node_id]
    assert isinstance(node, ForEachNode)
    items = await workflow_runs.list_items(session, run_id=parent.run_id, parent_step_id=parent.id)
    states = [
        ItemState(i.item_index or 0, i.status, i.output, i.error_class, i.error_message)
        for i in items
    ]
    nxt = items_next(node, states)
    events = list(before)
    by_index = {i.item_index: i for i in items}
    if nxt.start:
        ready: StepWrite = {"status": "ready", "ready_at": now}
        await workflow_runs.save_items(
            session, parent=parent, items=dict.fromkeys(nxt.start, ready)
        )
        for index in nxt.start:
            item = _item_of(by_index[index])
            events.append(
                Event(
                    "step.ready",
                    engine.item_key(parent.node_id, parent.visit, index),
                    {
                        "node": parent.node_id,
                        "visit": parent.visit,
                        "item": {"index": index, "label": item_label(item)},
                    },
                )
            )
        await _notify(session, READY_CHANNEL, str(parent.run_id))
    if nxt.cancel:
        events += await _cancel_items(session, parent, now=now, reason="too_many_failures")
    if nxt.outcome is None:
        await _apply(session, loaded, Transition((), tuple(events), None), now=now)
        return
    transition = engine.resolve(
        loaded.spec, loaded.views(), parent.node_id, parent.visit, nxt.outcome, loaded.facts
    )
    await _apply(session, loaded, transition, now=now, before=events)


async def _settle_item(
    session: AsyncSession,
    loaded: Loaded,
    item: StepRow,
    outcome: Outcome,
    *,
    now: datetime,
    before: Sequence[Event] = (),
) -> None:
    """An item's final outcome; an unknown outcome counts as a failed item."""
    assert item.parent_step_id is not None and item.item_index is not None
    key = key_of(item)
    if isinstance(outcome, engine.Succeeded):
        write = _ended("succeeded", now) | {
            "output": outcome.output,
            "summary": outcome.summary,
            "files": list(outcome.files),
        }
        event = Event("step.succeeded", key, {})
    else:
        error_class = outcome.error_class if isinstance(outcome, engine.Failed) else "unknown"
        message = _cut(outcome.message)
        write = _ended("failed", now) | {"error_class": error_class, "error_message": message}
        event = Event("step.failed", key, {"error_class": error_class, "message": message})
    parent = await workflow_runs.get_step(session, item.parent_step_id)
    assert parent is not None, "an item's For each outlives it"
    await workflow_runs.save_items(session, parent=parent, items={item.item_index: write})
    await _advance_items(session, loaded, parent, now=now, before=[*before, event])


async def _resolve(
    session: AsyncSession,
    loaded: Loaded,
    step: StepRow,
    outcome: Outcome,
    *,
    now: datetime,
    before: Sequence[Event] = (),
) -> None:
    """An executed step's final outcome: a node visit follows its edges, an item its For each."""
    if step.item_index is not None:
        await _settle_item(session, loaded, step, outcome, now=now, before=before)
        return
    transition = engine.resolve(
        loaded.spec, loaded.views(), step.node_id, step.visit, outcome, loaded.facts
    )
    await _apply(session, loaded, transition, now=now, before=before)


async def _write_step(
    session: AsyncSession,
    loaded: Loaded,
    step: StepRow,
    write: StepWrite,
    events: Sequence[Event],
    *,
    now: datetime,
) -> None:
    """One step's new state outside the engine (retry, block, park), with its events."""
    if step.item_index is None:
        await _apply(
            session,
            loaded,
            Transition((), tuple(events), None),
            now=now,
            writes={(step.node_id, step.visit): write},
        )
        return
    assert step.parent_step_id is not None
    parent = await workflow_runs.get_step(session, step.parent_step_id)
    assert parent is not None, "an item's For each outlives it"
    await workflow_runs.save_items(session, parent=parent, items={step.item_index: write})
    if write.get("status") == "ready":
        await _notify(session, READY_CHANNEL, str(step.run_id))
    await _apply(session, loaded, Transition((), tuple(events), None), now=now)


async def _queue_posts(
    session: AsyncSession, loaded: Loaded, end: RunEnd, views: list[StepView]
) -> list[Event]:
    """The Finish's post on success, the owner's notice on a failure."""
    run, wf, spec = loaded.run, loaded.workflow, loaded.spec
    if end.status == "succeeded":
        node_id = next((v.node_id for v in views if v.key == end.step), None)
        queued = (
            None
            if node_id is None
            else await delivery.queue_finish(
                session,
                run=run,
                workflow=wf,
                spec=spec,
                steps=views,
                facts=loaded.facts,
                node_id=node_id,
            )
        )
    else:
        queued = await delivery.queue_failure_notice(
            session, run=run, workflow=wf, spec=spec, end=end
        )
    return [] if queued is None else [queued]


def chain_depth(run: RunRow) -> int:
    """How many event or call starts led to this run."""
    depth = run.triggered_by.get("depth") if run.trigger_kind in ("event", "call") else 0
    return depth if isinstance(depth, int) else 0


async def _offer_finished(
    session: AsyncSession, loaded: Loaded, rows: Sequence[StepRow], end: RunEnd, now: datetime
) -> None:
    """Event triggers listening to this workflow get the finished run as an event; the
    starts loop filters and maps it. A chain stops at `MAX_CHAIN_DEPTH`."""
    run = loaded.run
    depth = chain_depth(run) + 1
    source = f"workflow:{run.workflow_id}"
    listeners = await workflow_triggers.event_triggers_for(
        session, tenant_id=run.tenant_id, source=source
    )
    output = _finish_output(loaded.spec, rows)
    event: dict[str, Any] = {
        "workflow": {"id": str(run.workflow_id), "name": loaded.workflow.name},
        "run": {
            "id": str(run.id),
            "number": run.number,
            "status": end.status,
            "outcome": end.outcome,
            "output": output,
        },
        "depth": depth,
    }
    await offer_event(session, run.tenant_id, source, run.concurrency_key, event, now)
    if not listeners or depth > MAX_CHAIN_DEPTH:
        return
    for trigger in listeners:
        await workflow_triggers.record_event(
            session,
            trigger=trigger,
            status="accepted",
            received_at=now,
            delivery_id=f"run:{run.id}",
            verified=True,
            payload=event,
        )
    await _notify(session, START_CHANNEL, str(run.id))


async def offer_event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source: str,
    key: str | None,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    """Hold an event for the event waits on `source` (and `key`); the waits loop matches it."""
    await workflow_waits.buffer_event(
        session,
        tenant_id=tenant_id,
        source=source,
        event_key=key,
        payload=payload,
        now=now,
        expires_at=now + EVENT_BUFFER,
    )
    await _notify(session, WAITS_CHANNEL, source)


def _finish_output(spec: Spec, rows: Sequence[StepRow]) -> JsonValue:
    """A run's output: what its last Finish visit produced."""
    finish = [r for r in rows if isinstance(spec.nodes.get(r.node_id), FinishNode)]
    return max(finish, key=lambda r: r.visit).output if finish else None


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


Conflict = Literal["queue", "skip", "replace", "debounce"]


def _active_limit(spec: Spec, settings: Settings, trigger_kind: str = "manual") -> int:
    """Active runs a key may hold: the spec's limit, else 1 when the spec sets concurrency
    or for schedules (a slot never overlaps the last), else the deployment's ceiling."""
    asked = spec.concurrency.limit if spec.concurrency and spec.concurrency.limit else None
    one = spec.concurrency is not None or trigger_kind == "schedule"
    default = 1 if one else settings.workflows.max_active_runs
    return min(asked or default, settings.workflows.max_active_runs)


def on_conflict(spec: Spec, trigger_kind: str) -> Conflict:
    """What a start does over the limit; a schedule slot skips unless the spec says."""
    asked = spec.concurrency.on_conflict if spec.concurrency else None
    return asked or ("skip" if trigger_kind == "schedule" else "queue")


def concurrency_key(spec: Spec, inputs: dict[str, Any], now: datetime) -> str | None:
    """The spec's key on these inputs; None holds the whole workflow as one key."""
    source = spec.concurrency.key if spec.concurrency else None
    if not source:
        return None
    context: dict[str, JsonValue] = {"inputs": inputs}
    value = (
        render(source, context, now=now) if "${{" in source else evaluate(source, context, now=now)
    )
    return (value if isinstance(value, str) else str(value))[:200]


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
        if start.mode == "test" and start.version is None:
            spec_document: dict[str, Any] = workflow.draft_spec
            version = workflow.live_version or 0
        elif workflow.status != "active":
            return Refusal("paused", "This workflow is paused. Ask its owner.")
        elif workflow.live_version is None:
            return Refusal("not_published", "Publish this workflow before running it.")
        else:
            version = start.version or (
                source.version if source and start.fork_of else workflow.live_version
            )
            row = await workflows.get_version(
                session, tenant_id=tenant_id, workflow_id=workflow.id, number=version
            )
            if row is None:
                return Refusal("not_found", f"Version {version} of this workflow is gone.")
            spec_document = row.spec
        source_steps = await workflow_runs.list_steps(session, source.id) if source else []
    spec = parse_spec(spec_document)
    given = start.inputs if start.inputs is not None else (source.inputs if source else {})
    inputs, problems = engine.resolve_inputs(spec, given)
    if problems:
        return Refusal("invalid_inputs", problems[0])
    if start.fork_of and start.fork_of[1] not in spec.nodes:
        return Refusal("not_found", "That step is not in this version.")
    try:
        key = concurrency_key(spec, inputs, now)
    except ExpressionError as err:
        return Refusal("invalid_inputs", f"The concurrency key failed: {err}")
    limit = _active_limit(spec, settings, start.trigger_kind)
    conflict = on_conflict(spec, start.trigger_kind)
    place = Place(tenant_id, workflow, spec, version, start.mode, start.run_as_account_id)
    cleared = await check(sm, settings, place, now=now)
    if isinstance(cleared, Refusal):
        return cleared
    if conflict == "replace":
        await _replace(sm, settings, workflow.id, key, limit, now)
    async with sm.begin() as session:
        await workflows.get_workflow(
            session, tenant_id=tenant_id, workflow_id=workflow.id, lock=True
        )
        active = await workflow_runs.count_active_runs(
            session, workflow_id=workflow.id, concurrency_key=key
        )
        if active >= limit and conflict == "skip":
            return Refusal("skipped", "A run is already going. This one was skipped.")
        queued_count = await workflow_runs.count_active_runs(
            session, workflow_id=workflow.id, concurrency_key=key, statuses=("queued",)
        )
        if active >= limit and queued_count >= QUEUE_LIMIT:
            return Refusal("queue_full", f"{QUEUE_LIMIT} runs are already waiting. Try later.")
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
            concurrency_key=key,
            budget_usd=_budget(spec, settings, start, cleared),
            deadline_at=_deadline(spec, settings, now),
            parent_run_id=start.parent_run_id,
            parent_step_id=start.parent_step_id,
            forked_from_run_id=start.fork_of[0] if start.fork_of else None,
            forked_from_step=start.fork_of[1] if start.fork_of else None,
            test_options=(
                {
                    "spec": spec_document,
                    "pinned": start.pinned,
                    "pinned_from": str(start.pinned_from) if start.pinned_from else None,
                }
                if start.mode == "test"
                else None
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
        if active < limit:
            await _begin(session, settings, run, now)
        else:
            await _notify(session, START_CHANNEL, str(run.id))
        result = await workflow_runs.get_run(session, run.id)
    assert result is not None
    return result


async def _replace(
    sm: Sessions,
    settings: Settings,
    workflow_id: uuid.UUID,
    key: str | None,
    limit: int,
    now: datetime,
) -> None:
    """`replace`: cancel the oldest active runs of the key until a new one fits."""
    async with sm() as session:
        active = await workflow_runs.active_run_ids(
            session, workflow_id=workflow_id, concurrency_key=key
        )
    end = RunEnd("cancelled", reason="replaced", message="A newer run replaced this one")
    for run_id in active[: max(len(active) - limit + 1, 0)]:
        await stop_run(sm, settings, run_id=run_id, end=end, now=now)


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
            "files": s.files,
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
        spec = await spec_of(session, run)
        active = await workflow_runs.count_active_runs(
            session, workflow_id=run.workflow_id, concurrency_key=run.concurrency_key
        )
        if active >= _active_limit(spec, settings, run.trigger_kind):
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
            events=[NewEvent("step.started", key_of(step), {"attempt": attempt})],
            actor=ENGINE,
            at=now,
        )
        await _notify(session, run_channel(run.tenant_id), str(run.id))
    return Claimed(run, step, attempt)


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
    node = node_of(loaded.spec, claimed.step)
    refusal = await check(
        sm,
        settings,
        loaded.place(),
        now=clock(),
        budget_usd=loaded.run.budget_usd,
        cost_usd=loaded.run.cost_usd,
    )
    outcome: Outcome | Park | Parked
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
            )
            | item_context(loaded.spec, claimed.step),
            loaded.place(),
        )
        try:
            outcome = await executor(work)
        except Exception as err:  # an executor bug fails its step, never the loop
            outcome = engine.Failed("fatal", _cut(f"{type(err).__name__}: {err}") or "")
    if isinstance(outcome, Park):
        await _park(sm, settings, claimed, outcome, now=clock())
        return
    if isinstance(outcome, Parked):
        await park(sm, settings, claimed, outcome, now=clock())
        return
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
        delay = engine.retry_delay_s(node_of(loaded.spec, step), number, outcome)
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
        await _resolve(session, loaded, step, outcome, now=now)
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
        key_of(step),
        {"after_s": int(delay.total_seconds()), "message": _cut(message)},
    )
    await _write_step(session, loaded, step, write, [event], now=now)


async def _block(
    sm: Sessions, settings: Settings, claimed: Claimed, refusal: Refusal, *, now: datetime
) -> None:
    """A spend gate refused the step: it waits `blocked` and the waits loop rechecks it."""
    park = Park(
        "blocked",
        expires_at=now + BLOCKED_FOR,
        remind_at=now + BLOCKED_RECHECK,
        shown={"reason": refusal.reason, "message": refusal.message},
    )
    await _park(sm, settings, claimed, park, now=now, blocked=refusal)


async def _park(
    sm: Sessions,
    settings: Settings,
    claimed: Claimed,
    park: Park,
    *,
    now: datetime,
    blocked: Refusal | None = None,
) -> None:
    """The step waits on `park`'s wait. Its attempt runs on (a question resumes it), except
    a blocked step's, which a recheck runs again."""
    async with sm.begin() as session:
        held = await _locked_step(session, claimed.step.id, claimed.step.generation, "running")
        if held is None:
            return
        run, step = held
        kind = park.kind
        if blocked:
            await workflow_runs.finish_attempt(
                session,
                step_id=step.id,
                status="abandoned",
                now=now,
                error_class="gate",
                error_message=blocked.message,
            )
        events: list[Event] = []
        if park.wait is None:
            wait = await workflow_runs.open_wait(
                session,
                tenant_id=run.tenant_id,
                run_id=run.id,
                step_id=step.id,
                kind=kind,
                expires_at=park.expires_at,
                remind_at=park.remind_at,
                shown=park.shown,
                addressees=park.addressees,
                deliver=park.deliver,
                min_approvals=park.min_approvals,
                event_source=park.event_source,
                event_key=park.event_key,
                now=now,
            )
            data: dict[str, JsonValue] = {
                "id": str(wait.id),
                "kind": kind,
                "addressees": list(park.addressees),
                "expires_at": park.expires_at.isoformat() if park.expires_at else None,
            }
            if attached := (park.shown or {}).get("attachments"):
                data["attachments"] = attached
            if blocked:
                data["reason"] = blocked.reason
            events.append(Event("wait.opened", key_of(step), data))
            if park.ask is not None:
                events += await asks.queue(
                    session,
                    run=run,
                    wait_id=wait.id,
                    addressees=park.addressees,
                    ask=park.ask,
                )
            if kind == "event":
                await _notify(session, WAITS_CHANNEL, str(wait.id))  # a held event may match
        write: StepWrite = {
            "status": "waiting",
            "waiting_reason": kind,
            "lease_owner": None,
            "lease_expires_at": None,
        }
        await _write_step(
            session, await _load(session, settings, run, now), step, write, events, now=now
        )


async def resume(
    session: AsyncSession,
    settings: Settings,
    *,
    run: RunRow,
    step: StepRow,
    outcome: Outcome | None,
    before: Sequence[Event],
    now: datetime,
    actor: str = ENGINE,
) -> None:
    """A waiting step moves on, run and step locked by the caller: settled by `outcome`,
    or ready again for its attempt to continue (an answered question) when it is None."""
    loaded = await _load(session, settings, run, now)
    if outcome is None:
        write: StepWrite = {"status": "ready", "ready_at": now, "waiting_reason": None}
        if step.item_index is not None:  # a For each item's answered question
            await _write_step(session, loaded, step, write, before, now=now)
            return
        await _apply(
            session,
            loaded,
            Transition((), (), None),
            now=now,
            actor=actor,
            before=before,
            writes={(step.node_id, step.visit): write},
        )
        return
    failed = isinstance(outcome, engine.Failed)
    await workflow_runs.finish_attempt(
        session,
        step_id=step.id,
        status="failed" if failed else "succeeded",
        now=now,
        error_class=outcome.error_class if isinstance(outcome, engine.Failed) else None,
        error_message=_cut(outcome.message) if isinstance(outcome, engine.Failed) else None,
    )
    if step.item_index is not None:
        await _settle_item(session, loaded, step, outcome, now=now, before=before)
        return
    transition = engine.resolve(
        loaded.spec, loaded.views(), step.node_id, step.visit, outcome, loaded.facts
    )
    await _apply(session, loaded, transition, now=now, actor=actor, before=before)


async def context_of(
    session: AsyncSession, settings: Settings, run: RunRow, node_id: str, now: datetime
) -> dict[str, JsonValue]:
    """What a node's expressions see in this run now."""
    loaded = await _load(session, settings, run, now)
    return engine.expression_context(
        loaded.spec, loaded.views(), node_id=node_id, facts=loaded.facts
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
            event = Event("wait.timed_out", key_of(step), {"wait": str(wait.id)})
            failed = engine.Failed("gate", message, reason=reason)
            await _resolve(session, loaded, step, failed, now=now, before=[event])
            return
        await workflow_runs.close_waits(session, status="completed", now=now, step_id=step.id)
        event = Event("wait.decided", key_of(step), {"wait": str(wait.id), "decision": "cleared"})
        write: StepWrite = {"status": "ready", "ready_at": now, "waiting_reason": None}
        await _write_step(session, loaded, step, write, [event], now=now)


async def reclaim(sm: Sessions, settings: Settings, *, step_id: uuid.UUID, now: datetime) -> None:
    """A lease ran out with its worker gone. An agent or code step whose session was saved
    keeps its attempt and is reattached to that session. Nothing sent yet, or an idempotent
    node: run it again. Otherwise a write may have happened, and the step is `unknown` for
    its owner."""
    async with sm.begin() as session:
        held = await _locked_step(session, step_id, None, "running")
        if held is None or (held[1].lease_expires_at and held[1].lease_expires_at > now):
            return
        run, step = held
        attempts = await workflow_runs.list_attempts(session, step_id)
        loaded = await _load(session, settings, run, now)
        node = node_of(loaded.spec, step)
        if isinstance(node, AgentNode | CodeNode) and attempts and attempts[-1].session_id:
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
        await _resolve(session, loaded, step, outcome, now=now)


async def park(
    sm: Sessions, settings: Settings, claimed: Claimed, parked: Parked, *, now: datetime
) -> None:
    """A Workflow step waits as `event` on the child run it started; its attempt stays open."""
    async with sm.begin() as session:
        held = await _locked_step(session, claimed.step.id, claimed.step.generation, "running")
        if held is None:
            return
        run, step = held
        write: StepWrite = {
            "status": "waiting",
            "waiting_reason": "event",
            "lease_owner": None,
            "lease_expires_at": None,
        }
        child: dict[str, JsonValue] = {"id": str(parked.child_run_id), "number": parked.number}
        data: dict[str, JsonValue] = {
            "id": str(parked.child_run_id),
            "kind": "event",
            "addressees": [],
            "run": child,
        }
        event = Event("wait.opened", key_of(step), data)
        await _write_step(
            session, await _load(session, settings, run, now), step, write, [event], now=now
        )
    await settle_from_child(sm, settings, step_id=claimed.step.id, now=now)


async def settle_from_child(
    sm: Sessions, settings: Settings, *, step_id: uuid.UUID, now: datetime
) -> bool:
    """A waiting Workflow step settles once its child run ended: the child's output, or its
    failure. False while the child is still going."""
    async with sm.begin() as session:
        held = await _locked_step(session, step_id, None, "waiting")
        child = None if held is None else await workflow_runs.latest_child(session, step_id)
        if held is None or child is None or child.status in workflow_runs.ACTIVE_RUN_STATES:
            return False
        run, step = held
        ran = f"Run {child.number} {child.status.replace('_', ' ')}"
        outcome: Outcome
        if child.status == "succeeded":
            rows = await workflow_runs.list_steps(session, child.id)
            output = _finish_output(await spec_of(session, child), rows)
            outcome = engine.Succeeded(output, summary=ran)
        else:
            outcome = engine.Failed("agent", child.failure_message or ran)
        await workflow_runs.finish_attempt(
            session,
            step_id=step_id,
            status="succeeded" if child.status == "succeeded" else "failed",
            now=now,
            error_class=None if child.status == "succeeded" else "agent",
            error_message=None if child.status == "succeeded" else _cut(ran),
        )
        data: dict[str, JsonValue] = {
            "decision": child.status,
            "run": {"id": str(child.id), "number": child.number},
        }
        event = Event("wait.decided", key_of(step), data)
        loaded = await _load(session, settings, run, now)
        await _resolve(session, loaded, step, outcome, now=now, before=[event])
        return True


async def sync_children(sm: Sessions, settings: Settings, *, now: datetime, limit: int = 50) -> int:
    """Workflow steps settle from child runs that ended; children of a step that ended (its
    run was cancelled or stopped) are cancelled. Returns how many it handled."""
    async with sm() as session:
        parents = await workflow_runs.parents_of_ended_children(session, limit=limit)
        orphans = await workflow_runs.orphaned_children(session, limit=limit)
    for step_id in parents:
        await settle_from_child(sm, settings, step_id=step_id, now=now)
    end = RunEnd("cancelled", reason="parent_stopped", message="The calling run stopped")
    for run_id in orphans:
        await stop_run(sm, settings, run_id=run_id, end=end, now=now)
    return len(parents) + len(orphans)


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
            "step.resolved_by", key_of(step), {"decision": decision, "account": str(account_id)}
        )
        await _apply(
            session, loaded, transition, now=now, actor=account_actor(account_id), before=[event]
        )
        return True


# --- the sweep loop ---


async def sweep(
    sm: Sessions,
    settings: Settings,
    *,
    now: datetime,
    retention_days: Callable[[uuid.UUID], Awaitable[int]],
) -> None:
    """Delete trigger events past the payload retention and finished runs past their
    workspace's retention.

    `retention_days(tenant_id)` awaits to the workspace's retention in days.
    """
    async with sm.begin() as session:
        before = now - timedelta(days=settings.workflows.payload_retention_days)
        await workflow_triggers.delete_events_before(session, before=before)
        tenant_ids = await workflow_runs.tenants_with_finished_runs(session)
    for tenant_id in tenant_ids:
        days = await retention_days(tenant_id)
        async with sm.begin() as session:
            old = await workflow_runs.finished_runs_before(
                session, tenant_id=tenant_id, before=now - timedelta(days=days), limit=500
            )
            await workflow_runs.delete_runs(session, old)
