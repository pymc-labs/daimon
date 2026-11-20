"""One eval item end to end: hold its expected cost under the run's cap, play its turns on
the eval surface through a `TurnPort`, collect what happened, check it and settle spend."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Final, Literal, Protocol

import structlog
from daimon.core.evals import spend
from daimon.core.evals.chat.events import ChatLog
from daimon.core.evals.chat.script import Presses, ScriptPlayer, Seen
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
    TranscriptResult,
    TranscriptTool,
    Usage,
    WorldCall,
    to_document,
)
from daimon.core.evals.history import turn_text
from daimon.core.evals.scoring.checks import check_item
from daimon.core.evals.world.dispatch import FakeWorld, issue_world_token
from daimon.core.evals.world.simulator import ModelSimulator
from daimon.core.host_calls import WAKE_SEED
from daimon.core.stores import eval_runs, eval_world, usage_events
from daimon.core.stores import evals as store
from daimon.core.turn.state import TextBlock
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

RESERVED: Final = "daimon.reserved_usd"
"""What each running item of a run holds against its cap, by item id (`ext`)."""
_OPEN: Final = ("approved", "running")
_STOPPED: Final = frozenset({"cancelled", "partial", "failed", "done"})


@dataclass
class ItemMeter:
    """An item's usage while it runs: its finished turns, and the running turn's so far
    (`live`, which the port sets for the turn). Read against the cap while turns run, and
    kept when a turn fails."""

    done: Usage = field(default_factory=Usage)
    live: Callable[[], Usage] | None = None
    unpriced: bool = False
    """A turn used tokens no known price covers: the agent's cost counts at the hold."""
    started_at: datetime | None = None
    """When the item's clock started (its time limit and sandbox cost run from here); the
    port clears it while the item waits for the shared environment and sets it when the pod
    starts."""

    def add(self, usage: Usage) -> None:
        self.unpriced |= _unpriced(usage)
        self.done = _add_usage(self.done, usage)

    def settle(self) -> None:
        """The running turn ended, however it ended: what it used is the item's."""
        if self.live is not None:
            live, self.live = self.live, None
            self.add(live())

    def usage(self) -> Usage:
        return self.done if self.live is None else _add_usage(self.done, self.live())

    def agent_usd(self, floor: float) -> float:
        """What the agent's turns cost so far; at least `floor` once one had no price."""
        live = self.live() if self.live is not None else Usage()
        known = _add_usage(self.done, live).cost_usd or 0.0
        return max(known, floor) if self.unpriced or _unpriced(live) else known


def _unpriced(usage: Usage) -> bool:
    return usage.cost_usd is None and bool(usage.input_tokens or usage.output_tokens)


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
    meter: ItemMeter


class TurnPort(Protocol):
    """Runs an item's turns: the first opens its session, follow-ups continue it. A turn
    reports what it uses to `request.meter`, while it runs and when it ends or fails."""

    async def turn(self, request: TurnRequest) -> None: ...
    async def close(self, item_id: uuid.UUID) -> None: ...


@dataclass(frozen=True)
class ItemDeps:
    sessions: async_sessionmaker[AsyncSession]
    turns: TurnPort
    world: FakeWorld
    sandbox_rate_usd_per_hour: float = 0.0
    poll_s: float = 5.0
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    presses: Presses | None = None
    requester: ModelSimulator | None = None
    """Credential presses and the simulated requester the script uses; none: not played."""
    max_turns: int = 12
    """Turns an item may take: its first, the script's follow-ups and approved calls' wakes."""
    time_limit: timedelta = timedelta(minutes=30)


@dataclass
class _Running:
    """What a running item shares with its watcher."""

    item: store.ItemRow
    estimate_usd: float
    """The item's expected cost: its first hold, and the floor of an unpriced agent cost."""
    hold_usd: float
    started_at: datetime
    cancel: asyncio.Event
    meter: ItemMeter = field(default_factory=ItemMeter)
    stopped: bool = False
    """Its run stopped: cancelled, or at its cap."""
    error: ItemError | None = None
    """Why the watcher ended it: its time limit, or another worker took it over."""


def _ext(run: Run) -> JsonObject:
    return dict(run.ext or {})


def _holds(run: Run) -> dict[str, float]:
    held = _ext(run).get(RESERVED)
    if not isinstance(held, dict):
        return {}
    return {str(k): float(v) for k, v in held.items() if isinstance(v, int | float)}  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]


def _reserved(run: Run) -> float:
    return sum(_holds(run).values())


def _spent(run: Run) -> float:
    return (run.spend.total_usd or 0.0) if run.spend else 0.0


def _held(run: Run, item_id: uuid.UUID, usd: float | None) -> Run:
    """The run with the item holding `usd` against its cap; None releases its hold."""
    holds = {k: v for k, v in _holds(run).items() if k != str(item_id)}
    if usd is not None:
        holds[str(item_id)] = usd
    return run.model_copy(update={"ext": {**_ext(run), RESERVED: holds}})


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


async def close_lost(
    sessions: async_sessionmaker[AsyncSession], lost: list[tuple[uuid.UUID, uuid.UUID]],
    clock: Callable[[], datetime],
) -> None:  # fmt: skip
    """Release the holds of items whose worker was lost for good, and end their runs."""
    for run_id, item_id in lost:
        async with sessions.begin() as session:
            await store.update_run(session, run_id=run_id, change=lambda d, i=item_id: to_document(
                _held(Run.model_validate(d), i, None)))  # fmt: skip
            await _end_run(session, run_id, clock)


async def _hold(deps: ItemDeps, item: store.ItemRow) -> tuple[Run, float] | None:
    """Hold the item's expected cost under the cap; None (item closed) when it may not run.
    Decided under the run's row lock, so concurrent holds and settlements all count."""
    refused: list[Literal["stopped", "cap"]] = []
    next_usd = 0.0
    now = deps.clock()
    async with deps.sessions.begin() as session:
        # Set when the run is approved; fixed while it is open.
        cap = await eval_runs.cap_of(session, run_id=item.run_id)

        def started(document: JsonObject) -> JsonObject:
            nonlocal next_usd
            run = Run.model_validate(document)
            next_usd = item_usd(run.estimate, item.arm_key) if run.estimate else 0.0
            if run.status in _STOPPED or cap is None:
                refused.append("stopped")
                return document
            others = _reserved(_held(run, item.id, None))
            if not fits(spent=_spent(run), reserved=others, next_usd=next_usd, cap=cap):
                refused.append("cap")
                return document
            held = _held(run, item.id, next_usd)
            return to_document(held.model_copy(update={
                "status": "running", "started_at": held.started_at or now,
            }))  # fmt: skip

        updated = await store.update_run(session, run_id=item.run_id, change=started)
        if refused == ["stopped"]:
            error = ItemError(kind="cancelled", message="the run stopped")
            await _close_item(session, item, "cancelled", error)
        elif refused == ["cap"]:
            why = "the next item would pass the spending cap"
            error = ItemError(kind="cap", message=why)
            await _close_item(session, item, "skipped", error)
            # Items already running finish; the run ends partial when the last one does.
            await eval_runs.close_items(session, run_id=item.run_id, status="skipped",
                                        error=to_document(error))  # fmt: skip
        if refused:
            await _end_run(session, item.run_id, deps.clock)
            return None
    return Run.model_validate(updated.document), next_usd


async def _close_item(
    session: AsyncSession, item: store.ItemRow, status: str, error: ItemError
) -> None:
    document: JsonObject = {**item.document, "status": status, "error": to_document(error)}
    await eval_runs.save_item(session, item_id=item.id, document=document, attempt=item.attempts)


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
            continue
        # The worker runs MCP tools as custom ones: the server is the one the tool stands for.
        server = block.mcp_server_name or surface.tool_servers.get(block.name)
        add(kind="tool_call", call_id=block.id, tool=TranscriptTool(
            name=block.name, server=server, surface="mcp" if server else "builtin",
            input=to_document_value(block.input),
        ))  # fmt: skip
        if block.status != "pending":
            content = [{"type": "text", "text": c.text}
                       for c in block.result_content or () if c.type == "text"]  # fmt: skip
            add(kind="tool_result", call_id=block.id,
                result={"content": content or None, "is_error": block.is_error})  # fmt: skip


def link_world_calls(
    events: list[TranscriptEvent], calls: list[WorldCall]
) -> list[TranscriptEvent]:
    """The transcript with each MCP tool call's result naming the world call that answered
    it (same server and tool, in order), so the call is counted once, with its effects."""
    free = [c for c in calls if c.surface == "mcp"]
    results = {e.call_id: e for e in events if e.kind == "tool_result"}
    linked: list[TranscriptEvent] = []
    for event in events:
        if event.kind == "tool_result":
            continue
        linked.append(event)
        tool = event.tool
        if event.kind != "tool_call" or tool is None:
            continue
        found = next((c for c in free if c.tool == tool.name and c.server == tool.server), None)
        if found is not None:
            free.remove(found)
        result = results.pop(event.call_id, None)
        if found is None and result is None:
            continue
        shown = result.result if result and result.result else TranscriptResult()
        if found is not None:
            shown = shown.model_copy(update={"world_call": found.seq, "origin": found.origin})
        linked.append(TranscriptEvent(seq=0, turn=event.turn, kind="tool_result",
                                      call_id=event.call_id, result=shown))  # fmt: skip
    return [e.model_copy(update={"seq": n}) for n, e in enumerate(linked, 1)]


def to_document_value(value: dict[str, object]) -> JsonObject:
    """A tool call's input as JSON (inputs are JSON already; anything else becomes text)."""
    return to_jsonable_python(value, fallback=str)


def _sandbox_usd(deps: ItemDeps, running: _Running, now: datetime) -> float:
    started = running.meter.started_at
    if started is None:
        return 0.0
    return (now - started).total_seconds() / 3600 * deps.sandbox_rate_usd_per_hour


async def _check(deps: ItemDeps, running: _Running) -> None:
    """One look at a running item: keep its claim, end it at its time limit or when its run
    stops, and grow its hold to what it has spent so far, stopping the run past its cap."""
    item, now = running.item, deps.clock()
    started = running.meter.started_at
    if not running.cancel.is_set() and started and now - started > deps.time_limit:
        running.error = ItemError(kind="timeout", message="the item ran past its time limit")
        running.cancel.set()
        return
    grown: float | None = None
    async with deps.sessions.begin() as session:
        if not await eval_runs.heartbeat(session, item_id=item.id, attempt=item.attempts,
                                         now=now):  # fmt: skip
            running.error = ItemError(kind="other", message="another worker took the item over")
            running.cancel.set()
            return
        if running.cancel.is_set():
            # Ending (or being closed and stored): only the claim is kept fresh.
            return
        simulator_usd = await usage_events.cost_for_session(
            session, tenant_id=EVAL_TENANT_ID, session_id=f"eval-item:{item.id}"
        )
        spent = (running.meter.agent_usd(running.estimate_usd) + simulator_usd
                 + _sandbox_usd(deps, running, now))  # fmt: skip
        over = False
        if spent > running.hold_usd:
            grown = spent
            cap = await eval_runs.cap_of(session, run_id=item.run_id)

            def grow(document: JsonObject) -> JsonObject:
                nonlocal over
                run = _held(Run.model_validate(document), item.id, spent)
                over = cap is not None and _spent(run) + _reserved(run) > cap
                return to_document(run)

            row = await store.update_run(session, run_id=item.run_id, change=grow)
            if over:
                await _stop_run(session, item.run_id, "the run passed its spending cap")
        else:
            row = await store.run_by_id(session, item.run_id)
    if grown is not None:
        running.hold_usd = grown
    if over or row is None or row.status in _STOPPED:
        running.stopped = True
        running.cancel.set()


async def _watch(deps: ItemDeps, running: _Running) -> None:
    """Check the item every `poll_s`, until cancelled: the claim stays fresh while it closes."""
    while True:
        await asyncio.sleep(deps.poll_s)
        try:
            await _check(deps, running)
        # A failed check (the database away, a bad row) is tried again: the item must keep its
        # watcher.
        except Exception:
            log.warning("eval_item.check_failed", item_id=str(running.item.id), exc_info=True)


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


@dataclass(frozen=True)
class _Loaded:
    run_row: store.RunRow
    suite_doc: JsonObject
    case: Case
    arm: Arm
    token: str


async def _load(deps: ItemDeps, item: store.ItemRow, run: Run) -> _Loaded:
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
    arm = next(a for a in run.arms if a.id == item.arm_key)
    return _Loaded(run_row, suite_doc, Case.model_validate(revision.document), arm, token)


async def run_item(deps: ItemDeps, item: store.ItemRow) -> RunItem | None:
    """Run one claimed item; its stored result, or None when the cap or the run stopped it
    (or its result could not be stored, and it ended in error)."""
    held = await _hold(deps, item)
    if held is None:
        return None
    run, reserved = held
    now = deps.clock()
    running = _Running(item, reserved, reserved, now, asyncio.Event())
    running.meter.started_at = now
    # Watching starts with the hold: setup, closing and storing are all under its claim.
    watcher = asyncio.create_task(_watch(deps, running))
    try:
        return await _run(deps, item, run, running)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def _run(deps: ItemDeps, item: store.ItemRow, run: Run, running: _Running) -> RunItem | None:
    try:
        loaded = await _load(deps, item, run)
    # Whatever the setup raises, the item ends in error and releases its hold.
    except Exception as exc:
        log.warning("eval_item.setup_failed", item_id=str(item.id), exc_info=True)
        await _fail(deps, running, ItemError(kind="other", message=type(exc).__name__))
        return None
    case, arm, cancel = loaded.case, loaded.arm, running.cancel
    chat = ChatLog()
    reply = deps.requester
    script = ScriptPlayer(
        case, chat, stop=cancel, presses=deps.presses,
        simulate_reply=None if reply is None else partial(reply.requester_reply, item.id, case),
    )  # fmt: skip
    surface = EvalSurfaceLifecycle(chat, script)
    transcript: list[TranscriptEvent] = []
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
            await deps.turns.turn(TurnRequest(
                item_id=item.id, tenant_id=item.tenant_id, case=case, arm=arm, text=text,
                follow_up=follow_up, world_token=loaded.token, surface=surface, cancel=cancel,
                meter=running.meter,
            ))  # fmt: skip
            _transcript(transcript, turn, text, surface)
            await surface.end_turn()
            if surface.error is not None:
                error = ItemError(kind="agent", message=type(surface.error).__name__)
                break
            if cancel.is_set():
                break
            turn += 1
            more = bool(surface.wakes) or bool(script.replies and follow_ups > 0)
            if more and turn >= deps.max_turns:
                # An approval loop (each approved call's wake calls again) ends here.
                error = ItemError(kind="cap", message="the item reached its turn limit")
                break
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
        running.meter.settle()
    try:
        await deps.turns.close(item.id)
    # The pod may still run: the item fails, and the port keeps what it shares closed.
    except Exception as exc:
        log.warning("eval_item.close_failed", item_id=str(item.id), exc_info=True)
        error = error or ItemError(kind="sandbox", message=type(exc).__name__)
    error = running.error or error
    try:
        suite = Suite.model_validate(loaded.suite_doc)
        return await _finish(deps, running, loaded.run_row, case, suite, arm, surface, transcript,
                             error)  # fmt: skip
    # A result that cannot be stored must not leave the item running, to be run again.
    except Exception as exc:
        log.exception("eval_item.finish_failed", item_id=str(item.id))
        await _fail(deps, running, ItemError(kind="other", message=type(exc).__name__))
        return None


async def _settle(session: AsyncSession, deps: ItemDeps, running: _Running, *, saved: bool) -> None:
    """Add what the item spent to its run and release its hold (unless another worker holds
    the item now), stopping the run past its cap and ending it when nothing is left."""
    item, now = running.item, deps.clock()
    simulator_usd = await usage_events.cost_for_session(
        session, tenant_id=EVAL_TENANT_ID, session_id=f"eval-item:{item.id}"
    )
    agent_usd = running.meter.agent_usd(running.estimate_usd)
    sandbox_usd = _sandbox_usd(deps, running, now)
    costs = (("agent", agent_usd), ("simulator", simulator_usd), ("sandbox", sandbox_usd))

    def settled(document: JsonObject) -> JsonObject:
        run = Run.model_validate(document)
        run = _held(run, item.id, None) if saved else run
        for category, usd in costs:
            if usd:
                run = spend.add_spend(run, category, usd)
        return to_document(run)

    updated = await store.update_run(session, run_id=item.run_id, change=settled)
    cap = await eval_runs.cap_of(session, run_id=item.run_id)
    if cap is not None and _spent(Run.model_validate(updated.document)) > cap:
        await _stop_run(session, item.run_id, "the run passed its spending cap")
    await _end_run(session, item.run_id, deps.clock)


async def _fail(deps: ItemDeps, running: _Running, error: ItemError) -> None:
    async with deps.sessions.begin() as session:
        document: JsonObject = {**running.item.document, "status": "error",
                                "error": to_document(error)}  # fmt: skip
        saved = await eval_runs.save_item(session, item_id=running.item.id, document=document,
                                          attempt=running.item.attempts)  # fmt: skip
        await _settle(session, deps, running, saved=saved)


async def _finish(
    deps: ItemDeps, running: _Running, run_row: store.RunRow, case: Case, suite: Suite, arm: Arm,
    surface: EvalSurfaceLifecycle, transcript: list[TranscriptEvent], error: ItemError | None,
) -> RunItem:  # fmt: skip
    item, finished_at, started_at = running.item, deps.clock(), running.started_at
    async with deps.sessions() as session:
        calls = await eval_world.list_calls(session, item_id=item.id)
        overlay = await eval_world.list_overlay(session, item_id=item.id)
    deps.world.forget(item.id)
    world_calls = [WorldCall.model_validate(c) for c in calls]
    status = "error" if error else ("cancelled" if running.stopped else "done")
    events = link_world_calls(transcript, world_calls)
    result = RunItem.model_validate({
        **item.document, "status": status, "error": to_document(error) if error else None,
        "started_at": started_at, "finished_at": finished_at,
        "transcript": to_document(EventStream(events=events)), "chat": surface.chat.events,
        "outputs": surface.outputs or None,
        "world_calls": world_calls or None,
        "usage": running.meter.usage().model_copy(update={
            "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        }),
        "enclosure": Enclosure(mode="enclosed", backend=arm.live.backend if arm.live else None,
                               simulated_calls=sum(c.origin == "simulated" for c in world_calls),
                               denied_calls=sum(c.origin == "denied" for c in world_calls)),
    })  # fmt: skip
    world = {(o.service, o.resource_key): o.data for o in overlay}
    scores = check_item(case, result, suite.tool_aliases or [], world=world, at=finished_at)
    async with deps.sessions.begin() as session:
        saved = await eval_runs.save_item(session, item_id=item.id, document=to_document(result),
                                          attempt=item.attempts)  # fmt: skip
        # The run's row lock first: scores reference it, and two items finishing together each
        # holding a share of it would wait on the other's upgrade.
        await _settle(session, deps, running, saved=saved)
        if saved:
            await store.put_scores(
                session, tenant_id=item.tenant_id, run_id=run_row.id,
                scores=[to_document(s) for s in scores],
            )  # fmt: skip
    return result
