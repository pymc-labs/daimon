"""What the workflows API returns for runs: summaries, the run page, ledger rows, steps.

The run page's state is the projection of the ledger, folded the way the web client folds
it (`runs.ts`), so a page loaded from here and then fed the live stream agrees with itself.
The ledger holds account ids, not names; names and display sentences are added at read time.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast

from daimon.core.stores import workflow_reads as reads
from daimon.core.stores import workflow_runs as runs_store
from daimon.core.stores import workflow_waits as waits_store
from daimon.core.stores import workflows as workflows_store
from daimon.core.stores.workflow_run_events import EventRow, list_events
from daimon.core.stores.workflow_runs import RunRow, StepRow
from daimon.core.stores.workflow_waits import Wait
from daimon.core.workflows import ledger
from daimon.core.workflows.engine import (
    RunFacts,
    StepView,
    expression_context,
    item_key,
    step_key,
)
from daimon.core.workflows.expressions import ExpressionError, Ref, render, template_refs
from daimon.core.workflows.spec import parse_spec
from daimon.core.workflows.views import Person, iso, obj, people_of, person, seq
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

ACTIVE = ("queued", "running", "waiting")
_KEY = re.compile(r"^([^#\[]+)")
ENDED_STEP = ("succeeded", "failed", "skipped", "cancelled")


# ---------- shapes ----------


class TriggerRef(BaseModel):
    kind: str
    id: str


class RunSummary(BaseModel):
    id: str
    number: int
    status: str
    label: str | None
    trigger: TriggerRef
    started_at: str
    duration_ms: int | None
    current_step: str | None
    cost_usd: float
    version: int
    test: bool
    step_title: str | None
    workflow: dict[str, str]
    waiting_reason: str | None
    triggered_by: Person | None
    redriven: bool


class Attempt(BaseModel):
    n: int
    status: str
    started_at: str
    duration_ms: int | None = None
    cost_usd: float = 0
    session: str | None = None
    error: dict[str, str] | None = None


class RunWait(BaseModel):
    id: str
    kind: str
    status: str
    addressees: list[Person]
    opened_at: str
    expires_at: str | None
    closed_at: str | None = None
    decision: str | None = None
    decider: Person | None = None
    sent: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    answer: dict[str, Any] | None = None


class StepRun(BaseModel):
    key: str
    node: str
    visit: int
    item: dict[str, Any] | None
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    cost_usd: float = 0
    route: str | None = None
    attempts: list[Attempt] = []
    wait: RunWait | None = None
    lane: str | None = None
    progress: dict[str, int] | None = None
    reason: str | None = None
    via: list[str] = []
    reused: Literal["fork", "pinned"] | None = None


class Lane(BaseModel):
    id: str
    agent: str
    session: str


class RunFailure(BaseModel):
    reason: str
    message: str
    step: str | None


class RunState(BaseModel):
    status: str = "queued"
    waiting_reason: str | None = None
    failure: RunFailure | None = None
    outcome: str | None = None
    ended_at: str | None = None
    cost_usd: float = 0
    steps: list[StepRun] = []
    lanes: list[Lane] = []
    current_step: str | None = None


class RunTest(BaseModel):
    live_writes: bool
    pinned: list[str]
    pinned_from: str | None


class RunDetail(RunSummary):
    failure: RunFailure | None
    outcome: str | None
    ended_at: str | None
    steps: list[StepRun]
    lanes: list[Lane]
    spec: dict[str, Any]
    inputs: dict[str, Any]
    untrusted_inputs: bool
    parent: dict[str, Any] | None
    delivery: str | None
    test_options: RunTest | None
    budget_usd: float
    deadline_at: str | None
    events: int
    head_hash: str


class RunEvent(BaseModel):
    seq: int
    at: str
    type: str
    step: str | None
    actor: str
    detail: str
    data: dict[str, Any]
    hash: str


class Verify(BaseModel):
    ok: bool
    events: int


# ---------- ledger rows ----------

_DETAIL: dict[str, str] = {
    "run.waiting": "Waiting",
    "run.resumed": "Resumed",
    "run.succeeded": "Succeeded",
    "run.failed": "Run failed",
    "run.cancelled": "Cancelled",
    "run.timed_out": "Deadline reached",
    "step.ready": "Ready",
    "step.skipped": "Skipped",
    "step.started": "Started",
    "step.succeeded": "Succeeded",
    "step.failed": "Failed",
    "step.cancelled": "Cancelled",
    "step.unknown": "Outcome unknown",
    "step.resolved_by": "Settled by its owner",
    "step.retry_scheduled": "Will retry",
    "step.brief_sent": "Brief sent",
    "step.output_recorded": "Output recorded",
    "step.contract_rejected": "Output rejected",
    "step.question_asked": "Asked a question",
    "step.nudged": "Nudged to finish",
    "wait.opened": "Waiting",
    "wait.posted": "Posted",
    "wait.reminded": "Reminder sent",
    "wait.answer_refused": "Answer refused",
    "wait.decided": "Decided",
    "wait.timed_out": "Timed out",
    "delivery.queued": "Delivery queued",
}
_TRIGGER = {
    "schedule": "Schedule",
    "webhook": "Webhook delivery",
    "event": "Event",
    "call": "Called by a workflow",
}
_WAITING = {
    "approval": "Waiting for approval",
    "question": "Waiting for an answer",
    "unknown": "Needs a decision",
    "event": "Waiting for an event",
}
_DECIDED = {"approve": "Approved", "reject": "Rejected", "cleared": "Cleared", "arrived": "Arrived"}
_DELIVERY = {"ask": "Ask queued", "finish": "Result queued", "notice": "Notice queued"}


def _name(value: object) -> str | None:
    """A hydrated person's name (`_hydrate`)."""
    return _str(value.get("name")) if isinstance(value, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def _detail(e: EventRow, data: Mapping[str, Any], actor: str, run: RunRow | None) -> str:
    """The ledger row in words, as the run page shows it."""
    text = data.get("message") or data.get("reason")
    by = _name(data.get("by")) or (actor if _account_of(e.actor) else None)
    match e.type:
        case "run.failed" | "step.failed" | "step.unknown" if isinstance(text, str):
            return text
        case "run.queued":
            trigger = obj(data.get("trigger")).get("kind") or data.get("trigger") or "manual"
            words = _TRIGGER.get(str(trigger), "Queued")
            if trigger == "manual":
                return f"Run by {by}" if by else "Started by hand"
            return words
        case "run.started" if run is not None:
            return "Test run on the draft" if run.mode == "test" else f"Version {run.version}"
        case "run.waiting":
            return _WAITING.get(str(data.get("reason")), "Waiting")
        case "run.cancelled" | "step.cancelled" if by:
            return f"Cancelled by {by}"
        case "run.redriven":
            return f"Retried from failure by {by}" if by else "Retried from failure"
        case "run.forked_from":
            return "Started from an earlier run"
        case "step.ready" if (visit := data.get("visit")) and isinstance(visit, int) and visit > 1:
            return f"Visit {visit}"
        case "step.started" if (n := data.get("attempt")) and isinstance(n, int):
            return f"Attempt {n}"
        case "step.skipped" if data.get("reason") == "max_visits":
            return "Visited too many times"
        case "step.route_chosen":
            return f"Chose {data.get('route')}"
        case "step.succeeded" if data.get("reused"):
            return "Reused pinned output" if data["reused"] == "pinned" else "Reused from a run"
        case "step.retry_scheduled" if isinstance(text, str):
            return f"{text.rstrip('.')}, retrying in {data.get('after_s', 0)}s"
        case "step.resolved_by":
            return f"Settled by {by or 'its owner'}: {data.get('decision', '')}".strip(": ")
        case "wait.decided":
            if data.get("answer"):
                return f"Answered by {by}" if by else "Answered"
            words = _DECIDED.get(str(data.get("decision")), str(data.get("decision") or "Decided"))
            return f"{words.capitalize()} by {by}" if by else words.capitalize()
        case "wait.opened":
            names = ", ".join(n for a in seq(data.get("addressees")) if (n := _name(a)))
            kind = data.get("kind")
            if names and kind in ("approval", "question"):
                return f"{'Approval from' if kind == 'approval' else 'Question for'} {names}"
            return _WAITING.get(str(kind), "Waiting")
        case "wait.posted":
            return f"Posted: {obj(data.get('sent')).get('where') or 'in chat'}"
        case "delivery.queued":
            return _DELIVERY.get(str(data.get("kind")), "Delivery queued")
        case _:
            pass
    return _DETAIL.get(e.type, e.type)


def _hydrate(value: object, names: Mapping[uuid.UUID, Person]) -> object:
    """An account id in `by` or `addressees` becomes the person; anything else is kept."""
    if isinstance(value, str):
        try:
            return person(names, uuid.UUID(value)).model_dump()
        except ValueError:
            return value
    return value


def _account_of(actor: str) -> uuid.UUID | None:
    kind, _, rest = actor.partition(":")
    try:
        return uuid.UUID(rest) if kind == "account" else None
    except ValueError:
        return None


def _wanted_accounts(events: Sequence[EventRow]) -> set[uuid.UUID]:
    found: set[uuid.UUID] = set()
    for e in events:
        if (a := _account_of(e.actor)) is not None:
            found.add(a)
        sent = obj(e.data.get("sent"))
        posted = sent.get("addressee") if sent else None
        raw = [e.data.get("by"), posted, *seq(e.data.get("addressees"))]
        for item in raw:
            if isinstance(item, str):
                try:
                    found.add(uuid.UUID(item))
                except ValueError:
                    continue
    return found


async def event_views(session: AsyncSession, run: RunRow, *, after_seq: int = 0) -> list[RunEvent]:
    """Ledger rows for the page, with names, and answers read from their waits (the ledger
    holds only the answer's hash)."""
    events = await list_events(session, run.id, after_seq=after_seq)
    names = await people_of(session, run.tenant_id, _wanted_accounts(events))
    answered = [
        uuid.UUID(str(e.data["id"]))
        for e in events
        if e.type == "wait.decided" and isinstance(e.data.get("answer"), dict)
    ]
    found = await waits_store.waits_by_id(session, answered)
    views = [event_view(e, names, found, run) for e in events]
    await _add_costs(session, run, views)
    return views


#: Rows that end an attempt carry its cost, as `runs.ts` `project` reads it.
_ENDS_ATTEMPT = {"step.succeeded", "step.failed", "step.retry_scheduled", "step.cancelled"}


async def _add_costs(session: AsyncSession, run: RunRow, views: Sequence[RunEvent]) -> None:
    """Each row that ends an attempt gets its cost, and a success its time taken."""
    ends = [v for v in views if v.type in _ENDS_ATTEMPT and v.step]
    if not ends:
        return
    by_key: dict[str, list[tuple[datetime, float]]] = {}
    for node, visit, index, started, cost in await runs_store.list_attempt_costs(session, run.id):
        key = step_key(node, visit) if index is None else item_key(node, visit, index)
        by_key.setdefault(key, []).append((started, float(cost)))
    for v in ends:
        at = datetime.fromisoformat(v.at)
        started, cost = next(
            ((s, c) for s, c in reversed(by_key.get(v.step or "", [])) if s <= at), (None, 0.0)
        )
        if cost:
            v.data["cost"] = cost
        if v.type == "step.succeeded" and started and not v.data.get("reused"):
            v.detail = f"Succeeded in {max(1, round((at - started).total_seconds()))}s"


def answer_view(wait: Wait, at: str, names: Mapping[uuid.UUID, Person]) -> dict[str, Any]:
    return {
        "by": person(names, wait.decided_by_account_id).model_dump(),
        "via": wait.decided_via or "web",
        "text": wait.answer_text,
        "values": wait.answer_values or {},
        "files": [file_view(wait.run_id, f) for f in wait.answer_files or []],
        "at": at,
    }


def sent_view(sent: Mapping[str, Any], names: Mapping[uuid.UUID, Person]) -> dict[str, Any]:
    """Where an ask was posted (`wait.posted`), in words."""
    who = sent.get("addressee")
    where = "In a thread"
    if sent.get("kind") == "dm" and isinstance(who, str):
        where = f"Direct message to {person(names, uuid.UUID(who)).name}"
    return {
        "platform": sent.get("platform"),
        "where": where,
        "link": sent.get("link"),
        "at": sent.get("at"),
    }


def event_view(
    e: EventRow,
    names: Mapping[uuid.UUID, Person],
    waits: Mapping[uuid.UUID, Wait] | None = None,
    run: RunRow | None = None,
) -> RunEvent:
    data = dict(e.data)
    answer = data.get("answer")
    if e.type == "wait.decided" and isinstance(answer, dict):
        wait = (waits or {}).get(uuid.UUID(str(data["id"])))
        data["answer"] = answer_view(wait, str(answer.get("at", "")), names) if wait else None  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    if e.type == "wait.posted" and isinstance(sent := data.get("sent"), dict):
        data["sent"] = sent_view(cast("dict[str, Any]", sent), names)
    if e.type == "wait.opened" and isinstance(attached := data.get("attachments"), list):
        data["attachments"] = [file_view(e.run_id, f) for f in cast("list[object]", attached)]
    if "by" in data:
        data["by"] = _hydrate(data["by"], names)
    if isinstance(data.get("addressees"), list):
        data["addressees"] = [_hydrate(a, names) for a in data["addressees"]]
    account = _account_of(e.actor)
    actor = person(names, account).name if account is not None else e.actor.capitalize()
    return RunEvent(
        seq=e.seq,
        at=iso(e.at) or "",
        type=e.type,
        step=e.step_key,
        actor=actor,
        detail=_detail(e, data, actor, run),
        data=data,
        hash=e.hash,
    )


def verify(run: RunRow, events: Sequence[EventRow]) -> Verify:
    return Verify(ok=ledger.verify(run.id, events).ok, events=len(events))


# ---------- projection ----------


def _ms(a: str, b: str) -> int:
    return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() * 1000)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _close(
    last: Attempt | None, e: RunEvent, status: str, error: dict[str, str] | None = None
) -> None:
    if last is not None and last.status == "running":
        last.status = status
        last.duration_ms = _ms(last.started_at, e.at)
        last.error = error


def _settle(st: StepRun | None, e: RunEvent, status: str) -> None:
    if st:
        st.status, st.ended_at = status, e.at


def project(events: Sequence[RunEvent]) -> RunState:
    """Fold ledger rows, oldest first, into the run's state (`runs.ts` `project`)."""
    s = RunState()
    by: dict[str, StepRun] = {}

    def end(status: str, at: str) -> None:
        s.status, s.waiting_reason, s.ended_at = status, None, at

    for e in events:
        d = e.data
        st = by.get(e.step) if e.step else None
        last = st.attempts[-1] if st and st.attempts else None
        cost = d.get("cost")
        if isinstance(cost, int | float) and cost:
            s.cost_usd += cost
            if st:
                st.cost_usd += cost
            if last:
                last.cost_usd += cost
        err = {
            "class": _str(d.get("class")) or "agent",
            "message": _str(d.get("message")) or e.detail,
        }

        match e.type:
            case "run.started" | "run.resumed" | "run.redriven":
                s.status, s.waiting_reason, s.failure, s.ended_at = "running", None, None, None
            case "run.waiting":
                s.status, s.waiting_reason = "waiting", _str(d.get("reason"))
            case "run.succeeded":
                end("succeeded", e.at)
                s.outcome = "nothing_to_do" if d.get("outcome") == "nothing_to_do" else "completed"
            case "run.failed":
                end("failed", e.at)
                s.failure = RunFailure(
                    reason=_str(d.get("reason")) or "step_failed",
                    message=_str(d.get("message")) or e.detail,
                    step=e.step or _str(d.get("step")),
                )
            case "run.cancelled":
                end("cancelled", e.at)
            case "run.timed_out":
                end("timed_out", e.at)
            case "step.ready":
                if st:
                    st.status, st.ended_at, st.reason = "ready", None, None
                elif e.step:
                    visit = d.get("visit")
                    via = d.get("via")
                    item = d.get("item")
                    node = StepRun(
                        key=e.step,
                        node=_str(d.get("node")) or e.step,
                        visit=visit if isinstance(visit, int) else 1,
                        item=obj(item) or None,
                        status="ready",
                        via=[v for v in seq(via) if isinstance(v, str)],
                    )
                    by[e.step] = node
                    s.steps.append(node)
            case "step.started" if st:
                st.status, st.reason = "running", None
                st.started_at = st.started_at or e.at
                st.attempts.append(
                    Attempt(
                        n=len(st.attempts) + 1,
                        status="running",
                        started_at=e.at,
                        session=_str(d.get("session")),
                    )
                )
                if isinstance(d.get("total"), int):
                    st.progress = {"done": 0, "total": d["total"]}
                if lane := _str(d.get("lane")):
                    st.lane = lane
                    if not any(x.id == lane for x in s.lanes):
                        s.lanes.append(
                            Lane(
                                id=lane,
                                agent=_str(d.get("agent")) or "",
                                session=_str(d.get("session")) or "",
                            )
                        )
            case "step.route_chosen" if st:
                st.route = _str(d.get("route"))
            case "step.retry_scheduled":
                _close(last, e, "failed", err)
            case "step.succeeded":
                _close(last, e, "succeeded")
                _settle(st, e, "succeeded")
                if st and d.get("reused"):
                    st.reused = "pinned" if d["reused"] == "pinned" else "fork"
            case "step.failed":
                _close(last, e, "failed", err)
                _settle(st, e, "failed")
                if st:
                    st.reason = err["message"]
            case "step.unknown":
                _close(last, e, "abandoned", {"class": "unknown", "message": err["message"]})
                if st:
                    st.status, st.reason = "unknown", err["message"]
            case "step.skipped":
                _settle(st, e, "skipped")
                if st:
                    st.reason = _str(d.get("reason"))
            case "step.cancelled":
                _close(last, e, "abandoned")
                _settle(st, e, "cancelled")
                if st and st.wait and st.wait.status == "waiting":
                    st.wait.status, st.wait.closed_at = "cancelled", e.at
            case "step.resolved_by" if st:
                st.reason = None
            case "wait.opened" if st:
                st.status = "waiting"
                addressees = d.get("addressees")
                st.wait = RunWait(
                    id=_str(d.get("id")) or "",
                    kind=_str(d.get("kind")) or "approval",
                    status="waiting",
                    addressees=[
                        Person.model_validate(a) for a in seq(addressees) if isinstance(a, dict)
                    ],
                    opened_at=e.at,
                    expires_at=_str(d.get("expires_at")),
                    attachments=d.get("attachments") or [],
                )
            case "wait.posted" if st and st.wait and isinstance(d.get("sent"), dict):
                st.wait.sent = [*st.wait.sent, d["sent"]]
            case "wait.decided" if st and st.wait:
                by_ = d.get("by")
                st.wait.status, st.wait.closed_at = "completed", e.at
                st.wait.decision = _str(d.get("decision"))
                st.wait.decider = Person.model_validate(by_) if isinstance(by_, dict) else None
                st.wait.answer = d.get("answer") if isinstance(d.get("answer"), dict) else None
            case "wait.timed_out" if st and st.wait:
                st.wait.status, st.wait.closed_at = "timed_out", e.at
                st.wait.decision = _str(d.get("route"))
            case _:
                pass
    for p in s.steps:
        if p.progress is None:
            continue
        items = [x for x in s.steps if x.item and x.key.startswith(f"{p.key}[")]
        p.progress = {
            "total": p.progress["total"],
            "done": sum(1 for x in items if x.status in ENDED_STEP),
        }
    live = [x for x in s.steps if not x.item and x.status in ("running", "waiting", "unknown")]
    current = next((x for x in live if x.status == "unknown"), live[-1] if live else None)
    s.current_step = current.key if current else None
    s.cost_usd = round(s.cost_usd, 2)
    return s


# ---------- summaries and the run page ----------


def title_of(node_id: str, spec: Mapping[str, Any]) -> str:
    node = obj(obj(spec.get("nodes")).get(node_id))
    return node.get("title") or node_id[:1].upper() + node_id[1:].replace("_", " ")


def node_of(key: str) -> str:
    match = _KEY.match(key)
    return match.group(1) if match else key


async def _spec_of(
    session: AsyncSession, run: RunRow, cache: dict[tuple[uuid.UUID, int], dict[str, Any]]
) -> dict[str, Any]:
    if run.mode == "test" and run.test_options is not None:
        return run.test_options["spec"]
    key = (run.workflow_id, run.version)
    if key not in cache:
        version = await workflows_store.get_version(
            session, tenant_id=run.tenant_id, workflow_id=run.workflow_id, number=run.version
        )
        cache[key] = version.spec if version else {}
    return cache[key]


def _triggered_by(run: RunRow) -> uuid.UUID | None:
    raw = run.triggered_by.get("account_id")
    try:
        return uuid.UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def started_by_me(run: RunRow, account_id: uuid.UUID) -> bool:
    return _triggered_by(run) == account_id or run.run_as_account_id == account_id


async def summaries(
    session: AsyncSession, runs: Sequence[RunRow], names_of: Mapping[uuid.UUID, str]
) -> list[RunSummary]:
    """Summaries of runs of any workflows; `names_of` maps workflow ids to names."""
    if not runs:
        return []
    current = await reads.current_steps(session, [r.id for r in runs])
    cache: dict[tuple[uuid.UUID, int], dict[str, Any]] = {}
    people = await people_of(session, runs[0].tenant_id, [_triggered_by(r) for r in runs])
    out: list[RunSummary] = []
    for run in runs:
        steps = current.get(run.id, [])
        unknown = next((s for s in steps if s.status == "unknown"), None)
        picked = unknown or (steps[-1] if steps else None)
        at = step_key(picked.node_id, picked.visit) if picked else run.failure_step
        spec = await _spec_of(session, run, cache)
        started = run.started_at or run.created_at
        out.append(
            RunSummary(
                id=str(run.id),
                number=run.number,
                status=run.status,
                label=run.label,
                trigger=TriggerRef(kind=run.trigger_kind, id=run.trigger_key),
                started_at=iso(started) or "",
                duration_ms=round((run.ended_at - started).total_seconds() * 1000)
                if run.ended_at
                else None,
                current_step=at,
                cost_usd=round(float(run.cost_usd), 2),
                version=run.version,
                test=run.mode == "test",
                step_title=title_of(node_of(at), spec) if at else None,
                workflow={"id": str(run.workflow_id), "name": names_of.get(run.workflow_id, "")},
                waiting_reason=run.waiting_reason,
                triggered_by=person(people, _triggered_by(run))
                if run.trigger_kind == "manual" and _triggered_by(run)
                else None,
                redriven=run.redrive_count > 0,
            )
        )
    return out


def _overlay(state: RunState, run: RunRow) -> RunState:
    """The run row is the truth for status and cost; the ledger fills in the steps."""
    state.status = run.status
    state.waiting_reason = run.waiting_reason
    state.ended_at = iso(run.ended_at)
    state.cost_usd = round(float(run.cost_usd), 2)
    state.outcome = run.outcome
    state.failure = (
        RunFailure(
            reason=run.failure_reason or "step_failed",
            message=run.failure_message or "",
            step=run.failure_step,
        )
        if run.status in ("failed", "timed_out") and run.failure_reason
        else None
    )
    return state


async def detail(session: AsyncSession, run: RunRow, workflow_name: str) -> RunDetail:
    events = await event_views(session, run)
    state = _overlay(project(events), run)
    (summary,) = await summaries(session, [run], {run.workflow_id: workflow_name})
    spec = await _spec_of(session, run, {})
    parent_id = run.forked_from_run_id or run.parent_run_id
    parent = (
        await runs_store.get_run(session, parent_id, tenant_id=run.tenant_id) if parent_id else None
    )
    options = run.test_options
    return RunDetail(
        **summary.model_dump(),
        failure=state.failure,
        outcome=state.outcome,
        ended_at=state.ended_at,
        steps=state.steps,
        lanes=state.lanes,
        spec=spec,
        inputs=run.inputs,
        untrusted_inputs=run.untrusted_inputs,
        parent={"id": str(parent.id), "number": parent.number} if parent else None,
        delivery=None,
        test_options=RunTest(
            live_writes=bool(options.get("live_writes")),
            pinned=list(options.get("pinned") or {}),
            pinned_from=options.get("pinned_from"),
        )
        if options is not None
        else None,
        budget_usd=float(run.budget_usd),
        deadline_at=iso(run.deadline_at),
        events=len(events),
        head_hash=events[-1].hash if events else ledger.GENESIS,
    )


# ---------- one step ----------


class Proposal(BaseModel):
    id: str
    tool: str
    summary: str
    status: str


class ActivityLine(BaseModel):
    at: str
    kind: str
    text: str
    tool: str | None = None


class StepDetail(BaseModel):
    key: str
    attempt: int
    agent: str | None
    model: str | None
    summary: str | None
    route: dict[str, str] | None
    files: list[dict[str, Any]]
    input: Any
    output: Any
    untrusted: list[str]
    rejections: list[dict[str, Any]]
    proposals: list[Proposal]
    activity: list[ActivityLine]
    plan: list[dict[str, str]]


def _view(s: StepRow) -> StepView:
    return StepView(
        s.node_id, s.visit, s.status, route=s.route, output=s.output, summary=s.summary,
        via=tuple(s.via), files=tuple(s.files),
    )  # fmt: skip


def _inputs_of(node: Mapping[str, Any]) -> dict[str, Any]:
    body = node.get("do") or node
    return dict(body.get("inputs") or body.get("args") or {})


async def step_detail(
    session: AsyncSession, run: RunRow, key: str, attempt: int | None, *, now: datetime
) -> StepDetail | None:
    steps = await runs_store.list_steps(session, run.id)
    node_id = node_of(key)
    match = re.search(r"#(\d+)", key)
    visit = int(match.group(1)) if match else 1
    item = re.search(r"\[(\d+)\]$", key)
    index = int(item.group(1)) if item else None
    rows = steps if index is None else await runs_store.list_items(session, run_id=run.id)
    step = next(
        (s for s in rows if (s.node_id, s.visit, s.item_index) == (node_id, visit, index)), None
    )
    if step is None:
        return None
    spec = await _spec_of(session, run, {})
    node = obj(obj(spec.get("nodes")).get(node_id))
    own: dict[str, Any] = {}
    if index is not None:
        own = {"item": obj(step.inputs).get("item"), "index": index}
        own |= {node["as"]: own["item"]} if isinstance(node.get("as"), str) else {}
        node = obj(node.get("do"))
    attempts = await runs_store.list_attempts(session, step.id)
    chosen = next((a for a in attempts if a.number == attempt), attempts[-1] if attempts else None)
    exprs = _inputs_of(node)
    facts = RunFacts(
        inputs=run.inputs,
        run={"id": str(run.id), "number": run.number, "mode": run.mode, "label": run.label},
        workflow={"id": str(run.workflow_id)},
        trigger={"kind": run.trigger_kind, "key": run.trigger_key},
        now=now,
    )
    context: Any = None
    try:
        context = expression_context(parse_spec(spec), [_view(s) for s in steps], node_id, facts)
        context |= own
    except ValueError:
        context = None
    shown: dict[str, Any] = {}
    for name, text in exprs.items():
        try:
            shown[name] = render(text, context, now=now) if context is not None else text
        except ExpressionError:
            shown[name] = None
    untrusted = [
        f"input.{name}"
        for name, text in exprs.items()
        if run.untrusted_inputs
        and isinstance(text, str)
        and any(r.path[:1] == ("inputs",) for r in _refs(text))
    ]
    events = await list_events(session, run.id)
    done = step.status == "succeeded"
    agent = node.get("agent") or obj(spec.get("defaults")).get("agent")
    route = step.route
    way = obj(node.get("routes")).get(route) if route else None
    activity = await runs_store.list_activity(session, step.id)
    return StepDetail(
        key=key,
        attempt=chosen.number if chosen else 0,
        agent=agent if node.get("kind") == "agent" else None,
        model=node.get("model") if node.get("kind") == "agent" else None,
        summary=step.summary if done else None,
        route={"name": route, "reason": str(obj(way).get("when") or "")} if route else None,
        files=[file_view(run.id, f) for f in step.files] if done else [],
        input=shown,
        output=step.output if done else None,
        untrusted=untrusted,
        rejections=[
            {"attempt": 1, "errors": list(e.data.get("errors") or [])}
            for e in events
            if e.type == "step.contract_rejected" and e.step_key == key
        ],
        proposals=[],
        activity=[
            ActivityLine(at=at.isoformat(), kind=kind, text=text, tool=tool)
            for at, kind, text, tool in activity
        ],
        plan=[],
    )


def file_view(run_id: uuid.UUID, ref: object) -> dict[str, Any]:
    """A step's file as the web shows it, downloaded through the run's files route."""
    file: dict[str, Any] = cast("dict[str, Any]", ref) if isinstance(ref, dict) else {}
    return {
        "id": file.get("file_id"),
        "name": file.get("filename"),
        "type": file.get("mime_type"),
        "size": file.get("size_bytes"),
        "url": f"/api/workflows/runs/{run_id}/files/{file.get('file_id')}",
    }


def _refs(text: str) -> list[Ref]:
    try:
        return template_refs(text)
    except ExpressionError:
        return []


def utcnow() -> datetime:
    return datetime.now(UTC)
