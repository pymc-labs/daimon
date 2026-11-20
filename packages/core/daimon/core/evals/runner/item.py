"""One eval item end to end: hold its expected cost under the run's cap, play its turns on
the eval surface through a `TurnPort`, collect what happened, check it and settle spend."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

import structlog
from daimon.core.evals import spend
from daimon.core.evals.chat.events import ChatLog
from daimon.core.evals.chat.script import ScriptPlayer, Seen
from daimon.core.evals.chat.surface import EvalSurfaceLifecycle
from daimon.core.evals.cost import fits, item_usd
from daimon.core.evals.eval_tenant import EVAL_TENANT_ID
from daimon.core.evals.format import (
    Arm,
    Case,
    Enclosure,
    EventStream,
    ItemError,
    JsonObject,
    Run,
    RunItem,
    Suite,
    TranscriptEvent,
    TranscriptTool,
    Usage,
    WorldCall,
    to_document,
)
from daimon.core.evals.history import turn_text
from daimon.core.evals.scoring.checks import check_item
from daimon.core.evals.world.dispatch import FakeWorld, issue_world_token
from daimon.core.host_calls import WAKE_SEED
from daimon.core.stores import eval_runs, eval_world, usage_events
from daimon.core.stores import evals as store
from daimon.core.turn.state import TextBlock
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

RESERVED: Final = "daimon.reserved_usd"
"""What running items of a run hold against its cap (`ext`)."""
_OPEN: Final = ("approved", "running")
_STOPPED: Final = frozenset({"cancelled", "partial", "failed", "done"})


@dataclass(frozen=True)
class TurnRequest:
    item_id: uuid.UUID
    tenant_id: uuid.UUID
    """The workspace the run belongs to; the turn itself runs in the eval tenant."""
    case: Case
    arm: Arm
    text: str
    follow_up: bool
    world_token: str
    surface: EvalSurfaceLifecycle
    cancel: asyncio.Event


class TurnPort(Protocol):
    """Runs an item's turns: the first opens its session, follow-ups continue it."""

    async def turn(self, request: TurnRequest) -> Usage: ...
    async def close(self, item_id: uuid.UUID) -> None: ...


@dataclass(frozen=True)
class ItemDeps:
    sessions: async_sessionmaker[AsyncSession]
    turns: TurnPort
    world: FakeWorld
    sandbox_rate_usd_per_hour: float = 0.0
    poll_s: float = 5.0
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def _ext(run: Run) -> JsonObject:
    return dict(run.ext or {})


def _reserved(run: Run) -> float:
    held = _ext(run).get(RESERVED)
    return float(held) if isinstance(held, int | float) else 0.0


def _held(run: Run, usd: float) -> Run:
    return run.model_copy(update={"ext": {**_ext(run), RESERVED: max(0.0, _reserved(run) + usd)}})


async def _end_run(session: AsyncSession, run_id: uuid.UUID, clock: Callable[[], datetime]) -> None:
    """Close a run once no item is left to run: done when every item is, else partial."""
    counts = await eval_runs.status_counts(session, run_id=run_id)
    if counts.get("pending") or counts.get("running"):
        return

    def ended(document: JsonObject) -> JsonObject:
        status = document["status"]
        if status in ("approved", "running"):
            status = "done" if set(counts) <= {"done"} else "partial"
        return {**document, "status": status, "finished_at": clock().isoformat()}

    await store.update_run(session, run_id=run_id, change=ended)


async def _stop_run(session: AsyncSession, run_id: uuid.UUID, why: str) -> None:
    """The cap is reached: no more items start, and running ones are interrupted."""
    await store.update_run(
        session, run_id=run_id,
        change=lambda d: {**d, "status": "partial"} if d["status"] in _OPEN else d,
    )  # fmt: skip
    await eval_runs.close_items(
        session, run_id=run_id, status="skipped",
        error=to_document(ItemError(kind="cap", message=why)),
    )  # fmt: skip


async def _hold(deps: ItemDeps, item: store.ItemRow) -> tuple[Run, float] | None:
    """Hold the item's expected cost under the cap; None (item closed) when it may not run."""
    async with deps.sessions.begin() as session:
        row = await store.run_by_id(session, item.run_id)
        assert row is not None
        run = Run.model_validate(row.document)
        cap = await eval_runs.cap_of(session, run_id=row.id)
        next_usd = item_usd(run.estimate, item.arm_key) if run.estimate else 0.0
        spent = (run.spend.total_usd or 0.0) if run.spend else 0.0
        if run.status in _STOPPED or cap is None:
            error = ItemError(kind="cancelled", message="the run stopped")
            await _close_item(session, item, "cancelled", error)
            await _end_run(session, row.id, deps.clock)
            return None
        if not fits(spent=spent, reserved=_reserved(run), next_usd=next_usd, cap=cap):
            await _close_item(session, item, "skipped", ItemError(kind="cap",
                              message="the next item would pass the spending cap"))  # fmt: skip
            await _stop_run(session, row.id, "the next item would pass the spending cap")
            await _end_run(session, row.id, deps.clock)
            return None
        now = deps.clock()

        def started(document: JsonObject) -> JsonObject:
            held = _held(Run.model_validate(document), next_usd)
            return to_document(held.model_copy(update={
                "status": "running", "started_at": held.started_at or now,
            }))  # fmt: skip

        updated = await store.update_run(session, run_id=row.id, change=started)
    return Run.model_validate(updated.document), next_usd


async def _close_item(
    session: AsyncSession, item: store.ItemRow, status: str, error: ItemError
) -> None:
    document: JsonObject = {**item.document, "status": status, "error": to_document(error)}
    await eval_runs.save_item(session, item_id=item.id, document=document)


def _transcript(
    events: list[TranscriptEvent], turn: int, text: str, surface: EvalSurfaceLifecycle
) -> None:
    def add(**fields: object) -> None:
        events.append(
            TranscriptEvent.model_validate({"seq": len(events) + 1, "turn": turn, **fields})
        )

    add(kind="user_message", text=text)
    for block in surface.state.content:
        if isinstance(block, TextBlock):
            add(kind="assistant_text", text=block.text)
        else:
            surface_kind = "mcp" if block.mcp_server_name else "builtin"
            add(kind="tool_call", call_id=block.id, tool=TranscriptTool(
                name=block.name, server=block.mcp_server_name, surface=surface_kind,
                input=to_document_value(block.input),
            ))  # fmt: skip


def to_document_value(value: dict[str, object]) -> JsonObject:
    """A tool call's input as JSON (inputs are JSON already; anything else becomes text)."""
    return to_jsonable_python(value, fallback=str)


async def _watch(
    deps: ItemDeps, item: store.ItemRow, cancel: asyncio.Event, stopped: asyncio.Event
) -> None:
    """Keep the claim fresh, and interrupt the item when its run stops."""
    while not cancel.is_set():
        await asyncio.sleep(deps.poll_s)
        async with deps.sessions.begin() as session:
            await eval_runs.heartbeat(session, item_id=item.id, now=deps.clock())
            row = await store.run_by_id(session, item.run_id)
        if row is None or row.status in _STOPPED:
            stopped.set()
            cancel.set()


def _add_usage(total: Usage, more: Usage) -> Usage:
    def plus(a: float | None, b: float | None) -> float | None:
        return None if a is None and b is None else (a or 0) + (b or 0)

    return Usage(
        input_tokens=int(plus(total.input_tokens, more.input_tokens) or 0) or None,
        output_tokens=int(plus(total.output_tokens, more.output_tokens) or 0) or None,
        cache_read_tokens=int(plus(total.cache_read_tokens, more.cache_read_tokens) or 0) or None,
        cache_write_tokens=int(plus(total.cache_write_tokens, more.cache_write_tokens) or 0)
        or None,
        cost_usd=plus(total.cost_usd, more.cost_usd),
        tool_calls=int(plus(total.tool_calls, more.tool_calls) or 0) or None,
    )


async def run_item(deps: ItemDeps, item: store.ItemRow) -> RunItem | None:
    """Run one claimed item; its stored result, or None when the cap or the run stopped it."""
    held = await _hold(deps, item)
    if held is None:
        return None
    run, reserved = held
    async with deps.sessions.begin() as session:
        run_row = await store.run_by_id(session, item.run_id)
        assert run_row is not None
        revision = await store.case_revision(
            session, suite_id=run_row.suite_id, key=item.case_key, revision=item.case_revision
        )
        suite_doc = await store.suite_document(
            session, suite_id=run_row.suite_id, version=run_row.suite_version
        )
        token = await issue_world_token(session, item.id)
    assert revision is not None and suite_doc is not None
    case = Case.model_validate(revision.document)
    arm = next(a for a in run.arms if a.id == item.arm_key)
    started_at = deps.clock()
    chat = ChatLog()
    script = ScriptPlayer(case, chat)
    surface = EvalSurfaceLifecycle(chat, script)
    cancel, stopped = script.stop, asyncio.Event()
    watcher = asyncio.create_task(_watch(deps, item, cancel, stopped))
    transcript: list[TranscriptEvent] = []
    usage = Usage()
    error: ItemError | None = None
    try:
        for message in case.turn.messages:
            chat.add("message", message.author, message_id=message.id, text=message.text,
                     format=message.format)  # fmt: skip
            surface.reply_to = message.id
        await script.happened(Seen("start"))
        text, follow_up, turn = turn_text(case), False, 0
        follow_ups = case.script.max_follow_ups if case.script else 0
        while True:
            surface.state, surface.error = type(surface.state)(), None
            usage = _add_usage(usage, await deps.turns.turn(TurnRequest(
                item_id=item.id, tenant_id=item.tenant_id, case=case, arm=arm, text=text,
                follow_up=follow_up, world_token=token, surface=surface, cancel=cancel,
            )))  # fmt: skip
            _transcript(transcript, turn, text, surface)
            await surface.end_turn()
            if surface.error is not None:
                error = ItemError(kind="agent", message=type(surface.error).__name__)
                break
            if cancel.is_set():
                break
            turn += 1
            if surface.wakes:
                # An approved card's call runs before the agent hears that it ran.
                text = f"{await surface.wakes.pop(0)()}\n\n{WAKE_SEED}"
            elif script.replies and follow_ups > 0:
                text, follow_ups = script.replies.pop(0), follow_ups - 1
            else:
                break
            follow_up = True
            surface.reply_to = chat.events[-1].message_id
    # One item's failure, whatever it is, must not stop the worker: it is the item's error.
    except Exception as exc:
        log.warning("eval_item.failed", item_id=str(item.id), exc_info=True)
        error = ItemError(kind="other", message=type(exc).__name__)
    finally:
        cancel.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await deps.turns.close(item.id)
    suite = Suite.model_validate(suite_doc)
    return await _finish(deps, item, run_row, case, suite, arm, reserved, started_at, surface,
                         transcript, usage, error, stopped.is_set())  # fmt: skip


async def _finish(
    deps: ItemDeps, item: store.ItemRow, run_row: store.RunRow, case: Case, suite: Suite, arm: Arm,
    reserved: float, started_at: datetime, surface: EvalSurfaceLifecycle,
    transcript: list[TranscriptEvent],
    usage: Usage, error: ItemError | None, stopped: bool,
) -> RunItem:  # fmt: skip
    finished_at = deps.clock()
    async with deps.sessions() as session:
        calls = await eval_world.list_calls(session, item_id=item.id)
        overlay = await eval_world.list_overlay(session, item_id=item.id)
        simulator_usd = await usage_events.cost_for_session(
            session, tenant_id=EVAL_TENANT_ID, session_id=f"eval-item:{item.id}"
        )
    deps.world.forget(item.id)
    sandbox_usd = (finished_at - started_at).total_seconds() / 3600 * deps.sandbox_rate_usd_per_hour
    world_calls = [WorldCall.model_validate(c) for c in calls]
    status = "error" if error else ("cancelled" if stopped else "done")
    result = RunItem.model_validate({
        **item.document, "status": status, "error": to_document(error) if error else None,
        "started_at": started_at, "finished_at": finished_at,
        "transcript": to_document(EventStream(events=transcript)), "chat": surface.chat.events,
        "outputs": surface.outputs or None,
        "world_calls": world_calls or None,
        "usage": usage.model_copy(update={
            "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        }),
        "enclosure": Enclosure(mode="enclosed", backend=arm.live.backend if arm.live else None,
                               simulated_calls=sum(c.origin == "simulated" for c in world_calls),
                               denied_calls=sum(c.origin == "denied" for c in world_calls)),
    })  # fmt: skip
    world = {(o.service, o.resource_key): o.data for o in overlay}
    scores = check_item(case, result, suite.tool_aliases or [], world=world, at=finished_at)
    agent_usd = usage.cost_usd or 0.0
    async with deps.sessions.begin() as session:
        await eval_runs.save_item(session, item_id=item.id, document=to_document(result))
        await store.put_scores(
            session, tenant_id=item.tenant_id, run_id=run_row.id,
            scores=[to_document(s) for s in scores],
        )  # fmt: skip

        def settled(document: JsonObject) -> JsonObject:
            run = _held(Run.model_validate(document), -reserved)
            for category, usd in (("agent", agent_usd), ("simulator", simulator_usd),
                                  ("sandbox", sandbox_usd)):  # fmt: skip
                if usd:
                    run = spend.add_spend(run, category, usd)
            return to_document(run)

        updated = await store.update_run(session, run_id=run_row.id, change=settled)
        cap = await eval_runs.cap_of(session, run_id=run_row.id)
        total = updated.document.get("spend") or {}
        spent = total.get("total_usd") if isinstance(total, dict) else None  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        if cap is not None and isinstance(spent, int | float) and spent > cap:
            await _stop_run(session, run_row.id, "the run passed its spending cap")
        await _end_run(session, run_row.id, deps.clock)
    return result
