"""Polls: a trigger or a wait that calls another service's read tool on an interval.

A poll trigger turns each new (or, with `updated`, changed) row of the result into an event
the starts loop runs, so every run still starts through `runs.start_run`. The first poll only
records a baseline. A poll wait checks its `until` expression over the result and continues
the run when it holds. Both read through the same `PollClient`, which applies the watch
poller's checks (the operator's `POLLABLE_TOOLS` list, a read-only listing, the schema), and
neither makes a model call. The result is outside data: rows become untrusted inputs.

One poll failing never stops the loop: each trigger and wait has its own boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

import structlog
from daimon.core.config import Settings
from daimon.core.continuity.watch_projection import (
    MAX_KEYS,
    SOURCE,
    project,
    value_digest,
)
from daimon.core.stores import workflow_runs
from daimon.core.stores import workflow_triggers as store
from daimon.core.stores.workflow_triggers import TriggerRow
from daimon.core.workflows import gate, runs, waits
from daimon.core.workflows.expressions import ExpressionError, render
from daimon.core.workflows.runs import Sessions
from daimon.core.workflows.spec import PollTrigger, WaitNode, parse_duration
from daimon.core.workflows.triggers import MappingError, map_inputs, passes
from pydantic import JsonValue
from sqlalchemy import func, select

log = structlog.get_logger(__name__)

#: Triggers, waits and started rows one pass handles; the rest wait for the next pass.
_BATCH: Final = 50
_PAUSE_TEXT: Final = {
    "tool_unlisted": "{what} stopped: its tool is no longer on the list of pollable tools.",
    "source_changed": "{what} stopped: its tool changed. Publish the workflow again to accept it.",
    "credential_missing": "{what} stopped: the agent has no key for that service. Add one.",
}


class PollPaused(Exception):
    """The poll cannot go on as approved. `reason` is a key of `_PAUSE_TEXT`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PollFailed(Exception):
    """One read failed; the next interval tries again. The text is for logs, not people."""


@dataclass(frozen=True)
class Polled:
    """A read's result and the digest of the tool's listing it was read under."""

    result: JsonValue
    digest: str


class PollClient(Protocol):
    async def read(
        self, tenant_id: uuid.UUID, agent: str, tool: str, args: dict[str, JsonValue], /
    ) -> Polled:
        """Call the read tool `server/tool` as `agent`. Raises `PollPaused` or `PollFailed`."""
        ...


async def _read(
    client: PollClient,
    tenant_id: uuid.UUID,
    agent: str,
    tool: str,
    args: dict[str, JsonValue],
) -> Polled:
    """The one place a poll reaches a tool. The client decides the call (reads only, through
    `decide_unattended_call`) against the tool's live listing before it sends anything."""
    polled = await client.read(tenant_id, agent, tool, args)
    project(polled.result, ".", limits=SOURCE)  # refuses a result past the bounds
    return polled


# --- the trigger ---


@dataclass(frozen=True)
class _Row:
    key: str
    content: str
    item: JsonValue


def _rows(result: JsonValue, config: PollTrigger) -> list[_Row]:
    """The result's rows with their key and content digests; one row per key."""
    found = project(result, config.items or ".", limits=SOURCE)
    if not isinstance(found, list):
        raise PollFailed("The result has no list of rows")
    key_path = config.key
    watch = config.watch or []
    rows: dict[str, _Row] = {}
    for item in found:
        identity = project(item, key_path, limits=SOURCE)
        if identity is None:
            continue  # a row without a key cannot be tracked
        watched: JsonValue = [project(item, path, limits=SOURCE) for path in watch] or item
        key = value_digest(identity)
        rows.setdefault(key, _Row(key, value_digest(watched)[:12], item))
    if len(rows) > MAX_KEYS:
        raise PollFailed(f"A poll tracks at most {MAX_KEYS} rows")
    return list(rows.values())


def _delivery(trigger: TriggerRow, row: _Row, config: PollTrigger) -> str:
    """A key alone, unless `updated` counts each change: an added row that leaves a listing
    and returns edited is not new."""
    base = f"poll:{trigger.id}:{row.key}"
    return base if config.once or config.rule != "updated" else f"{base}:{row.content}"


async def poll_triggers(
    sm: Sessions, settings: Settings, client: PollClient, *, now: datetime
) -> int:
    """Poll the due triggers and record their new rows as events; returns the events."""
    async with sm.begin() as session:
        due = await store.claim_due_polls(session, now=now, limit=_BATCH)
    recorded = 0
    for trigger, _ in due:
        try:
            recorded += await _poll_trigger(sm, settings, client, trigger, now)
        except PollPaused as paused:
            await _pause(sm, trigger, paused.reason)
        except PollFailed as failed:
            log.warning("workflow_poll.failed", trigger_id=str(trigger.id), error=str(failed))
            await _note(sm, trigger, "failed", now)
        except Exception as err:
            log.exception(
                "workflow_poll.crashed", trigger_id=str(trigger.id), error=type(err).__name__
            )
    if recorded:
        async with sm.begin() as session:
            await session.execute(select(func.pg_notify(runs.START_CHANNEL, "poll")))
    return recorded


async def _poll_trigger(
    sm: Sessions, settings: Settings, client: PollClient, trigger: TriggerRow, now: datetime
) -> int:
    features = await gate.features_at(sm, settings, trigger.tenant_id)
    if features is None or not (features.enabled and features.polls):
        return 0
    live = trigger.live
    assert live is not None, "claimed polls are live"
    config = PollTrigger.model_validate(live)
    try:
        args = render(dict(config.args or {}), {"trigger": {"id": config.id}}, now=now)
    except ExpressionError as err:
        raise PollFailed(f"The arguments failed: {err}") from err
    assert isinstance(args, dict)
    polled = await _read(client, trigger.tenant_id, config.agent, config.tool, args)
    rows = _rows(polled.result, config)
    async with sm.begin() as session:
        current = await store.get_trigger(session, trigger.id, lock=True)
        if current is None or not current.enabled or current.live != live:
            return 0  # republished or paused while the read ran
        state = dict(current.state or {})
        pinned = state.get("digest")
        if pinned is not None and pinned != polled.digest:
            raise PollPaused("source_changed")
        seen: dict[str, str] = state.get("seen") or {}
        fresh: dict[str, str] = {}
        recorded = 0
        for row in rows:
            old = seen.get(row.key)
            if recorded >= _BATCH:  # the rest start on the next pass
                if old is not None:
                    fresh[row.key] = old
                continue
            fresh[row.key] = row.content if config.rule == "updated" else (old or row.content)
            if "seen" not in state:
                continue  # the first poll only sets the baseline
            if old is not None and (config.rule != "updated" or old == row.content):
                continue
            recorded += await _record(session, current, config, row, now)
        state |= {"seen": fresh, "digest": polled.digest, "polled_at": now.isoformat()}
        state.pop("error", None)
        await store.set_poll_state(session, trigger.id, state)
        return recorded


async def _record(
    session: Any,  # noqa: ANN401 - the open transaction
    trigger: TriggerRow,
    config: PollTrigger,
    row: _Row,
    now: datetime,
) -> int:
    """One row's event: skipped by `start_when`, rejected when a template fails, else
    accepted (a repeat of its delivery id is recorded as a duplicate and starts nothing)."""
    context: dict[str, JsonValue] = {
        "item": row.item,
        "payload": row.item,
        "trigger": {"id": config.id},
    }
    delivery = _delivery(trigger, row, config)
    try:
        if not passes(config.start_when, context, now=now):
            return 0
        inputs = map_inputs(config.inputs, context, now=now)
    except MappingError as err:
        log.info("workflow_poll.row_rejected", trigger_id=str(trigger.id), error=str(err))
        await store.record_event(
            session,
            trigger=trigger,
            status="rejected",
            received_at=now,
            delivery_id=delivery,
            verified=True,
            reason="invalid_inputs",
        )
        return 0
    _, first = await store.record_event(
        session,
        trigger=trigger,
        status="accepted",
        received_at=now,
        delivery_id=delivery,
        verified=True,
        payload={"item": row.item},
        inputs=inputs,
    )
    return 0 if first is not None else 1


async def _pause(sm: Sessions, trigger: TriggerRow, reason: str) -> None:
    async with sm.begin() as session:
        current = await store.get_trigger(session, trigger.id, lock=True)
        if current is None or not current.enabled:
            return
        name = f"The {trigger.trigger_key} poll"
        await store.pause_poll(
            session, current, reason=reason, text=_PAUSE_TEXT[reason].format(what=name)
        )
    log.warning("workflow_poll.paused", trigger_id=str(trigger.id), reason=reason)


async def _note(sm: Sessions, trigger: TriggerRow, error: str, now: datetime) -> None:
    """Keep the last failure in the trigger's state, where its page can show it."""
    async with sm.begin() as session:
        current = await store.get_trigger(session, trigger.id, lock=True)
        if current is not None:
            state = {**(current.state or {}), "error": error, "failed_at": now.isoformat()}
            await store.set_poll_state(session, trigger.id, state)


# --- the wait ---


async def poll_waits(sm: Sessions, settings: Settings, client: PollClient, *, now: datetime) -> int:
    """Check the due poll waits; returns how many arrived. A wait past its timeout is left to
    the waits loop's expiry."""
    async with sm() as session:
        due = await workflow_runs.due_poll_waits(session, now=now, limit=_BATCH)
    arrived = 0
    for wait in due:
        try:
            arrived += await _check_wait(sm, settings, client, wait, now)
        except PollPaused as paused:
            text = _PAUSE_TEXT[paused.reason].format(what="This poll wait")
            await waits.settle_poll(sm, settings, wait, failure=text, now=now)
        except PollFailed as failed:
            log.warning("workflow_wait_poll.failed", wait_id=str(wait.id), error=str(failed))
            await _later(sm, wait, now)
        except Exception as err:
            log.exception(
                "workflow_wait_poll.crashed", wait_id=str(wait.id), error=type(err).__name__
            )
    return arrived


async def _later(
    sm: Sessions, wait: workflow_runs.WaitRow, now: datetime, every: str = "1m"
) -> None:
    async with sm.begin() as session:
        await workflow_runs.set_next_check(session, wait.id, now + parse_duration(every))


async def _check_wait(
    sm: Sessions, settings: Settings, client: PollClient, wait: workflow_runs.WaitRow, now: datetime
) -> int:
    async with sm() as session:
        run = await workflow_runs.get_run(session, wait.run_id)
        step = await workflow_runs.get_step(session, wait.step_id)
        if run is None or step is None:
            return 0
        node = (await runs.spec_of(session, run)).nodes.get(step.node_id)
        if not isinstance(node, WaitNode) or node.poll is None:
            return 0
        context = await runs.context_of(session, settings, run, step.node_id, now)
    poll = node.poll
    features = await gate.features_at(sm, settings, run.tenant_id)
    if features is None or not (features.enabled and features.polls):
        return int(
            await waits.settle_poll(sm, settings, wait, failure="Polls are off here.", now=now)
        )
    try:
        args = render(dict(poll.args or {}), context, now=now)
    except ExpressionError as err:
        return int(await waits.settle_poll(sm, settings, wait, failure=str(err), now=now))
    assert isinstance(args, dict)
    polled = await _read(client, run.tenant_id, poll.agent, poll.tool, args)
    try:
        done = passes(poll.until, {**context, "result": polled.result}, now=now)
    except MappingError as err:
        return int(await waits.settle_poll(sm, settings, wait, failure=str(err), now=now))
    if done:
        return int(await waits.settle_poll(sm, settings, wait, result=polled.result, now=now))
    await _later(sm, wait, now, poll.every)
    return 0
