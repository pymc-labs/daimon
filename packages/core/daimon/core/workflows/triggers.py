"""Triggers on the engine's side: schedule slots become events, and accepted events become
runs through `runs.start_run`, the one way any trigger starts a run.

An event is a webhook delivery (already verified and mapped by `ingress`), a schedule
slot or another workflow's finished run. Here each passes the trigger's filter, debounce
and daily spend cap, then starts a live run as the live version's publisher, or a test
run of the draft as the editor who listened. Payload values are untrusted inputs.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from daimon.core.config import Settings
from daimon.core.stores import workflow_triggers as store
from daimon.core.stores import workflows
from daimon.core.stores.workflow_runs import RunRow
from daimon.core.stores.workflow_triggers import EventRow, TriggerRow
from daimon.core.workflows import runs
from daimon.core.workflows.expressions import ExpressionError, evaluate, render
from daimon.core.workflows.preflight import Refusal
from daimon.core.workflows.runs import Sessions
from daimon.core.workflows.spec import CallTrigger, Spec, parse_duration, parse_spec
from pydantic import JsonValue, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: A slot older than this when the loop sees it is missed (`catch_up` decides).
FRESHNESS: Final = timedelta(minutes=15)
#: The daily spend cap of a webhook or event trigger that names none.
DEFAULT_SPEND_CAP_USD: Final = Decimal(20)
_UNTRUSTED: Final = frozenset({"webhook", "event"})
_BATCH: Final = 50


class MappingError(ValueError):
    """A trigger's mapping or filter failed on one payload; the text is for the person."""


def map_inputs(
    template: JsonValue, context: Mapping[str, JsonValue], *, now: datetime
) -> dict[str, JsonValue]:
    """The run's inputs from a trigger's mapping; without one the payload is the inputs."""
    if template is None:
        value = context.get("payload", context.get("event"))
    else:
        try:
            value = render(template, context, now=now)
        except ExpressionError as err:
            raise MappingError(str(err)) from err
    if not isinstance(value, dict):
        raise MappingError("The mapping must give an object of inputs")
    return value


def passes(source: str | None, context: Mapping[str, JsonValue], *, now: datetime) -> bool:
    """A trigger's `start_when` or `match` filter; none passes everything."""
    if not source:
        return True
    try:
        if "${{" in source:
            return render(source, context, now=now) is True
        return evaluate(source, context, now=now) is True
    except ExpressionError as err:
        raise MappingError(str(err)) from err


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), time(), tzinfo=now.tzinfo)


# --- the schedules loop ---


async def fire_schedules(sm: Sessions, *, now: datetime) -> int:
    """Due slots become accepted events keyed by slot, so a slot never fires twice."""
    async with sm.begin() as session:
        due = await store.claim_due_schedules(session, now=now, max_age=FRESHNESS, limit=_BATCH)
        for trigger, slot in due:
            spec = trigger.live or {}
            given = spec.get("with")
            await store.record_event(
                session,
                trigger=trigger,
                status="accepted",
                received_at=now,
                delivery_id=f"slot:{slot.isoformat()}",
                verified=True,
                inputs=given if isinstance(given, dict) else {},  # pyright: ignore[reportUnknownArgumentType]  # JSONB
            )
        if due:
            await session.execute(select(func.pg_notify(runs.START_CHANNEL, "schedule")))
    return len(due)


# --- the starts loop ---


async def _spec_for(
    session: AsyncSession, trigger: TriggerRow, test: bool
) -> tuple[Spec, uuid.UUID | None] | None:
    """The spec an event starts (draft for a test) and who the live run runs as."""
    workflow = await workflows.get_workflow(
        session, tenant_id=trigger.tenant_id, workflow_id=trigger.workflow_id
    )
    if workflow is None:
        return None
    if test:
        try:
            return parse_spec(workflow.draft_spec), trigger.listener
        except ValidationError:
            return None
    if workflow.live_version is None:
        return None
    live = await workflows.get_version(
        session, tenant_id=trigger.tenant_id, workflow_id=workflow.id, number=workflow.live_version
    )
    assert live is not None, "the live version exists"
    return parse_spec(live.spec), live.published_by_account_id


def _trigger_spec(spec: Spec, key: str) -> dict[str, Any]:
    found = next((t for t in spec.triggers or [] if t.id == key), None)
    return found.model_dump(by_alias=True, exclude_none=True) if found else {}


async def _hold(
    session: AsyncSession, spec: Spec, event: EventRow, inputs: dict[str, Any], now: datetime
) -> str | None:
    """`debounce`: wait until the key is quiet. 'later' keeps the event, 'superseded'
    skips it for a newer one with the same key, None starts it."""
    concurrency = spec.concurrency
    if concurrency is None or concurrency.on_conflict != "debounce" or event.test:
        return None
    quiet = parse_duration(concurrency.debounce) if concurrency.debounce else timedelta(minutes=1)
    key = runs.concurrency_key(spec, inputs, now)
    for newer in await store.accepted_after(session, event):
        if runs.concurrency_key(spec, newer.inputs or {}, now) == key:
            return "superseded"
    return "later" if event.received_at + quiet > now else None


async def _over_cap(
    session: AsyncSession, trigger: TriggerRow, config: Mapping[str, Any], now: datetime
) -> bool:
    """The trigger's runs today reached its cap; the owner hears once a day."""
    asked = config.get("spend_cap_usd")
    cap = Decimal(str(asked)) if isinstance(asked, int | float) else DEFAULT_SPEND_CAP_USD
    since = _day_start(now)
    spent = await store.committed_spend(
        session, workflow_id=trigger.workflow_id, trigger_key=trigger.trigger_key, since=since
    )
    if spent < cap:
        return False
    topic = f"spend_cap:{trigger.trigger_key}"
    if not await store.notice_sent_since(
        session, workflow_id=trigger.workflow_id, topic=topic, since=since
    ):
        await store.add_owner_notice(
            session,
            tenant_id=trigger.tenant_id,
            workflow_id=trigger.workflow_id,
            topic=topic,
            text=(
                f"The {trigger.trigger_key} trigger reached its ${cap} daily limit and is "
                "paused until tomorrow. Raise its limit to start more runs today."
            ),
        )
    return True


async def _start_one(
    sm: Sessions,
    settings: Settings,
    session: AsyncSession,
    event: EventRow,
    trigger: TriggerRow,
    now: datetime,
) -> RunRow | Refusal | str | None:
    """A run, a refusal, a skip reason, or None to keep the event for later."""
    found = await _spec_for(session, trigger, event.test)
    if found is None:
        return Refusal("not_published", "No live version")
    spec, run_as = found
    config = _trigger_spec(spec, trigger.trigger_key)
    if not config or config.get("kind") != trigger.kind:
        return Refusal("not_found", "This trigger is not in the version")
    inputs = event.inputs or {}
    depth = 0
    if trigger.kind == "event":
        payload: dict[str, JsonValue] = event.payload or {}
        context: dict[str, JsonValue] = {"event": payload, "trigger": {"id": trigger.trigger_key}}
        try:
            if not passes(config.get("match"), context, now=now):
                return "no_match"
            inputs = map_inputs(config.get("inputs"), context, now=now)
        except MappingError as err:
            return Refusal("invalid_inputs", str(err))
        found_depth = payload.get("depth")
        depth = found_depth if isinstance(found_depth, int) else 1
    try:
        held = await _hold(session, spec, event, inputs, now)
    except ExpressionError as err:
        return Refusal("invalid_inputs", f"The concurrency key failed: {err}")
    if held is not None:
        return None if held == "later" else held
    if (
        not event.test
        and trigger.kind in _UNTRUSTED
        and await _over_cap(session, trigger, config, now)
    ):
        return "spend_cap"
    triggered_by: dict[str, Any] = {
        "kind": trigger.kind,
        "trigger_id": str(trigger.id),
        "event_id": str(event.id),
    }
    if trigger.kind == "event":
        triggered_by["depth"] = depth
    return await runs.start_run(
        sm,
        settings,
        tenant_id=trigger.tenant_id,
        start=runs.Start(
            workflow_id=trigger.workflow_id,
            run_as_account_id=run_as,
            triggered_by=triggered_by,
            trigger_kind=trigger.kind,
            trigger_key=trigger.trigger_key,
            inputs=inputs,
            mode="test" if event.test else "live",
            untrusted_inputs=trigger.kind in _UNTRUSTED,
        ),
        now=now,
    )


async def start_from_events(sm: Sessions, settings: Settings, *, now: datetime) -> int:
    """Turn accepted trigger events into runs; each is marked started, skipped or rejected."""
    started = 0
    async with sm.begin() as session:
        for event, trigger in await store.claim_accepted_events(session, limit=_BATCH):
            result = await _start_one(sm, settings, session, event, trigger, now)
            if result is None:
                continue
            if isinstance(result, str):
                await store.mark_event(session, event.id, status="skipped", reason=result)
            elif isinstance(result, Refusal):
                status = "skipped" if result.reason == "skipped" else "rejected"
                await store.mark_event(session, event.id, status=status, reason=result.reason)
            else:
                started += 1
                await store.mark_event(session, event.id, status="started", run_id=result.id)
    return started


# --- the call trigger ---


async def start_called(
    sm: Sessions,
    settings: Settings,
    *,
    parent: RunRow,
    workflow_id: uuid.UUID,
    inputs: dict[str, Any],
    now: datetime,
) -> RunRow | Refusal:
    """A child run of a workflow with a call trigger, from a Workflow node of `parent`."""
    depth = runs.chain_depth(parent) + 1
    if depth > runs.MAX_CHAIN_DEPTH:
        return Refusal(
            "too_deep", f"Workflows can call others {runs.MAX_CHAIN_DEPTH} deep at most."
        )
    async with sm() as session:
        target = await workflows.get_workflow(
            session, tenant_id=parent.tenant_id, workflow_id=workflow_id
        )
        live = (
            await workflows.get_version(
                session,
                tenant_id=parent.tenant_id,
                workflow_id=workflow_id,
                number=target.live_version,
            )
            if target is not None and target.live_version is not None
            else None
        )
    call = (
        next((t for t in parse_spec(live.spec).triggers or [] if isinstance(t, CallTrigger)), None)
        if live
        else None
    )
    if live is None or call is None:
        return Refusal("not_callable", "That workflow is not published with a call trigger.")
    return await runs.start_run(
        sm,
        settings,
        tenant_id=parent.tenant_id,
        start=runs.Start(
            workflow_id=workflow_id,
            run_as_account_id=live.published_by_account_id,
            triggered_by={"kind": "call", "run_id": str(parent.id), "depth": depth},
            trigger_kind="call",
            trigger_key=call.id,
            inputs=inputs,
            untrusted_inputs=parent.untrusted_inputs,
            parent_run_id=parent.id,
        ),
        now=now,
    )
