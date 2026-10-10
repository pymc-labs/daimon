"""Host wiring proofs: actual SDK bytes, durable claims and restart fencing."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from anthropic.types.beta.sessions import BetaManagedAgentsEventParams
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.driver import run_turn
from daimon.core.turn.io import default_mux_turn_io
from daimon.core.turn.persistence import TurnPersistence, UncertainSend
from daimon.core.turn.posture import BillingExempt
from daimon.testing.factories import make_tenant
from daimon.testing.ma import list_response, send_events_response
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import ProviderBinding
from mux.errors import OperationConflict, ScopeViolation
from mux.state.lease import LeaseBusy, StaleFence
from mux.state.memory import MemoryStateStore, SimulatedCrash
from mux.state.store import StateStore, binding_slot
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import make_agent_message, make_status_idle

NOW = datetime(2026, 10, 10, tzinfo=UTC)
TENANT = uuid.UUID(int=4104)
SCOPE = Scope(
    tenant_id=str(TENANT), account_id="caller", principal_id="daimon", authorization_id="admitted"
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="anthropic",
    account_scope_id="provider",
    tenant_id=SCOPE.tenant_id,
    account_id=SCOPE.account_id,
)
BINDING = ProviderBinding(
    id="binding",
    thread=ThreadRef(
        channel=ChannelRef(tenant_id=str(TENANT), platform="slack", channel_id="channel"),
        thread_id="thread",
    ),
    provider="anthropic",
    profile="anthropic.managed_agents",
    native_refs={"session": "session"},
    generation=1,
    config_revision=0,
    legacy_account_id=SCOPE.account_id,
)


@pytest_asyncio.fixture(params=("memory", "postgres"))
async def stores(
    request: pytest.FixtureRequest,
    db_engine: AsyncEngine,
    db_clean: None,
) -> AsyncIterator[Callable[[], StateStore]]:
    if request.param == "memory":
        store = MemoryStateStore()
        yield store.restart
    else:
        sessions = async_sessionmaker(db_engine, expire_on_commit=False)
        async with sessions() as db, db.begin():
            await make_tenant(db, id=TENANT, workspace_id="n4-a4")
        yield lambda: PostgresStateStore(sessions, trust_caller_clock=True)


def context(store: StateStore, *, key: str = "root", holder: str | None = None) -> TurnPersistence:
    return TurnPersistence(store, BINDING, SCOPE, operation_key=key, holder=holder, now=lambda: NOW)


def message(*, preview: bool = False) -> Event:
    return Event(
        id="same-source",
        session_id=SESSION.id,
        sequence=0,
        type="native.preview" if preview else "native.message",
        observed_at=NOW,
        authority="preview" if preview else "record",
        payload={},
        native=NativeProvenance(
            provider="anthropic", api_revision="test", event_id="same-source", cursor="cursor"
        ),
    )


async def test_actual_sdk_traffic_and_effects_match_legacy_with_durable_journal(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    raw = [
        make_agent_message(event_id="message", text="answer").model_dump(mode="json"),
        make_status_idle(event_id="ended").model_dump(mode="json"),
    ]
    runs: list[tuple[object, ...]] = []
    for path in ("legacy", "mux"):
        transport = ScriptedTransport()
        transport.queue(
            ScriptedReply.stream("/v1/sessions/session/events/stream", raw),
            ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
        )
        lifecycle = RecordingLifecycle()
        async with transport.client() as client:
            with context(store).activate():
                state = await run_turn(
                    anthropic=client,
                    session_id="session",
                    user_message="question",
                    lifecycle=lifecycle,
                    cancel=asyncio.Event(),
                    billing=BillingExempt(reason="headless-unrecorded"),
                    path=path,
                    scope=SCOPE,
                    now=lambda: NOW,
                    render_interval_s=3600,
                )
        transport.assert_consumed()
        runs.append((transport.requests, state.content, state.stop_reason, state.error, lifecycle))
        if path == "legacy":
            assert not await store.read_events(SESSION.id)
            assert await store.get_operation(SCOPE, "root:send:0") is None
    # Lifecycle instances are separate; compare their recorded effects.
    assert runs[0][:4] == runs[1][:4]
    assert vars(runs[0][4]) == vars(runs[1][4])
    events = await stores().read_events(SESSION.id)
    assert [event.sequence for event in events] == list(range(len(events)))
    assert [event.native.event_id for event in events] == ["message", "ended"]
    assert any(event.type == "session.turn_ended" for event in events)
    projection = await store.projection(SESSION.id)
    assert projection is not None and projection.cursor == "ended"
    operation = await store.get_operation(SCOPE, "root:send:0")
    assert operation is not None and operation.operation.status == "accepted"
    # Returning from the driver releases its lease.
    lease = await store.acquire_lease(
        binding_slot(BINDING), holder="successor", turn_id="next", now=NOW, ttl=timedelta(minutes=1)
    )
    assert lease.fence == 2 and not lease.took_over


async def test_replay_overlap_preserves_journal_sequence_and_preview_authority(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    first = context(store)

    async def append() -> None:
        await first.record(SESSION, message(preview=True))
        await first.record(SESSION, message())
        await first.record(SESSION, message())

    await first.run(append)
    restarted = context(stores())

    async def replay() -> None:
        await restarted.record(SESSION, message())

    await restarted.run(replay)
    events = await store.read_events(SESSION.id)
    assert [(event.sequence, event.authority) for event in events] == [
        (0, "preview"),
        (1, "record"),
    ]
    projection = await store.projection(SESSION.id)
    assert projection is not None and projection.cursor == "cursor"


async def test_replay_and_stream_overlap_reach_one_actual_sdk_journal(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    native = make_agent_message(event_id="overlap", text="once").model_dump(mode="json")
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("GET", "/v1/sessions/session/events", list_response([native])),
        ScriptedReply.stream("/v1/sessions/session/events/stream", [native]),
    )
    persistence = context(store)
    async with transport.client() as client:
        io = default_mux_turn_io(
            client, SCOPE, SESSION.id, read_timeout_s=120, persistence=persistence
        )

        async def pump() -> None:
            replay = await io.replay()
            assert [event.id for event in replay] == ["overlap"]
            stream = await io.open_stream(read_timeout_s=120)
            try:
                streamed = [event async for event in stream]
                assert [
                    event.normalized.id for event in streamed if event.normalized is not None
                ] == ["overlap"]
            finally:
                await stream.close()

        await persistence.run(pump)
    transport.assert_consumed()
    events = await store.read_events(SESSION.id)
    assert [(event.sequence, event.type) for event in events] == [
        (0, "agent.message"),
        (1, "session.history_gap"),
    ]
    assert events[0].native.event_id == "overlap"
    assert events[1].authority == "gap"
    projection = await store.projection(SESSION.id)
    assert projection is not None and projection.cursor == "overlap"


async def test_two_senders_sharing_one_lease_still_have_one_claim(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    entered, release = asyncio.Event(), asyncio.Event()
    sends: list[str] = []

    async def deliver(key: str) -> SendReceipt:
        sends.append(key)
        entered.set()
        await release.wait()
        return SendReceipt(operation_id=key, status="processed", input_ids=("input",))

    persistence = context(store, holder="worker")

    async def send() -> SendReceipt:
        return await persistence.mutate(
            SESSION, "send", {"text": "question"}, deliver, SendReceipt, lambda receipt: "processed"
        )

    async def pump() -> SendReceipt:
        task = asyncio.create_task(send())
        await entered.wait()
        try:
            with pytest.raises(UncertainSend):
                await send()
        finally:
            release.set()
        return await task

    receipt = await persistence.run(pump)
    assert sends == ["root:send:0"] and receipt.input_ids == ("input",)
    record = await store.get_operation(SCOPE, "root:send:0")
    assert record is not None and record.operation.status == "processed"


async def test_acknowledged_restart_restores_receipt_and_changed_bytes_conflict(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    sends: list[str] = []

    async def deliver(key: str) -> SendReceipt:
        sends.append(key)
        return SendReceipt(operation_id=key, status="processed", input_ids=("upstream-input",))

    async def send(persistence: TurnPersistence, text: str = "same") -> SendReceipt:
        return await persistence.mutate(
            SESSION, "send", {"text": text}, deliver, SendReceipt, lambda receipt: "processed"
        )

    first = context(store)
    expected = await first.run(lambda: send(first))
    restarted = context(stores())
    assert await restarted.run(lambda: send(restarted)) == expected
    with pytest.raises(OperationConflict):
        await restarted.run(lambda: send(restarted, "changed"))
    assert sends == ["root:send:0"]


@pytest.mark.parametrize(
    ("method", "point", "expected", "calls"),
    (
        ("claim_send", "before_commit", "pending", 0),
        ("claim_send", "after_commit", "sent", 0),
        ("advance_operation", "before_commit", "sent", 1),
        ("advance_operation", "after_commit", "processed", 1),
    ),
)
async def test_crash_boundaries_restart_without_duplicate_provider_delivery(
    method: str,
    point: str,
    expected: str,
    calls: int,
) -> None:
    from typing import Literal, cast

    store = MemoryStateStore()
    await store.put_binding(BINDING, expected_generation=0)
    crashed = store.restart(crash={method: cast(Literal["before_commit", "after_commit"], point)})
    sends: list[str] = []

    async def deliver(key: str) -> SendReceipt:
        sends.append(key)
        return SendReceipt(operation_id=key, status="processed", input_ids=("input",))

    async def send(persistence: TurnPersistence) -> SendReceipt:
        return await persistence.mutate(
            SESSION, "send", {"text": "same"}, deliver, SendReceipt, lambda receipt: "processed"
        )

    first = context(crashed)
    with pytest.raises(SimulatedCrash):
        await first.run(lambda: send(first))
    saved = await store.get_operation(SCOPE, "root:send:0")
    assert saved is not None and saved.operation.status == expected
    assert len(sends) == calls
    successor = context(store.restart())
    if expected == "sent":
        with pytest.raises(UncertainSend):
            await successor.run(lambda: send(successor))
        assert len(sends) == calls
    else:
        receipt = await successor.run(lambda: send(successor))
        assert receipt.input_ids == ("input",)
        assert len(sends) == 1


async def test_crash_after_journal_commit_replay_does_not_append_twice() -> None:
    store = MemoryStateStore()
    await store.put_binding(BINDING, expected_generation=0)
    crashed = context(store.restart(crash={"append_events": "after_commit"}))
    with pytest.raises(SimulatedCrash):
        await crashed.run(lambda: crashed.record(SESSION, message()))
    restarted = context(store.restart())
    await restarted.run(lambda: restarted.record(SESSION, message()))
    events = await store.read_events(SESSION.id)
    assert len(events) == 1 and events[0].sequence == 0
    projection = await store.projection(SESSION.id)
    assert projection is not None and projection.cursor == "cursor"


async def test_live_foreign_holder_is_refused_before_provider_io(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    await store.acquire_lease(
        binding_slot(BINDING),
        holder="other",
        turn_id="other-root",
        now=NOW,
        ttl=timedelta(minutes=5),
    )
    called = False

    async def pump() -> None:
        nonlocal called
        called = True

    with pytest.raises(LeaseBusy):
        await context(store).run(pump)
    assert not called
    assert await store.get_operation(SCOPE, "root:send:0") is None


async def test_lease_loss_cancels_pump_and_stale_writer_cannot_journal() -> None:
    store = MemoryStateStore()
    await store.put_binding(BINDING, expected_generation=0)
    clock = [NOW]
    persistence = TurnPersistence(
        store,
        BINDING,
        SCOPE,
        operation_key="root",
        now=lambda: clock[0],
        ttl=timedelta(seconds=1),
        send_timeout_s=0.1,
        renew_interval_s=0.01,
    )
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def pump() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(persistence.run(pump))
    await entered.wait()
    old = next(iter(store.data.leases.values())).active
    assert old is not None
    clock[0] += timedelta(seconds=2)
    successor = await store.acquire_lease(
        binding_slot(BINDING),
        holder="successor",
        turn_id="new",
        now=clock[0],
        ttl=timedelta(seconds=5),
    )
    with pytest.raises(StaleFence):
        await task
    assert cancelled.is_set() and successor.took_over and successor.fence == old.fence + 1
    from mux.state.journal import JournalEntry

    with pytest.raises(StaleFence):
        await store.append_events(
            SESSION,
            (JournalEntry(source_key="late", event=message()),),
            fence=old,
            cursor="late",
            now=clock[0],
        )
    assert not await store.read_events(SESSION.id)


async def test_journal_owner_must_be_persisted_and_scope_must_match(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    called = False

    async def pump() -> None:
        nonlocal called
        called = True

    with pytest.raises(ScopeViolation):
        await context(store).run(pump)
    assert not called
    foreign = SCOPE.model_copy(update={"tenant_id": str(uuid.UUID(int=999))})
    with pytest.raises(ScopeViolation):
        TurnPersistence(store, BINDING, foreign, operation_key="foreign")
    persistence = context(store)
    with pytest.raises(ScopeViolation):
        persistence.check_session(SCOPE, SESSION.model_copy(update={"id": "foreign"}))
    assert not await store.read_events(SESSION.id)


async def test_uncertain_sdk_delivery_restarts_by_replay_without_another_post(
    stores: Callable[[], StateStore],
) -> None:
    import httpx
    from daimon.core.turn.io import TurnConnectionLost

    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    inputs: tuple[BetaManagedAgentsEventParams, ...] = (
        {"type": "user.message", "content": [{"type": "text", "text": "question"}]},
    )
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST", "/v1/sessions/session/events", httpx.ReadError("lost acknowledgement")
        )
    )
    persistence = context(store)
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        io = default_mux_turn_io(
            client, SCOPE, SESSION.id, read_timeout_s=120, persistence=persistence
        )
        with pytest.raises(TurnConnectionLost):
            await persistence.run(lambda: io.send(inputs))
    transport.assert_consumed()
    record = await store.get_operation(SCOPE, "root:send:0")
    assert record is not None and record.operation.status == "outcome_unknown"
    replayed = ScriptedTransport()
    native = make_agent_message(event_id="accepted-message", text="answer").model_dump(mode="json")
    replayed.queue(ScriptedReply("GET", "/v1/sessions/session/events", list_response([native])))
    successor = context(stores())
    async with replayed.client(max_retries=MA_MAX_RETRIES) as client:
        io = default_mux_turn_io(
            client, SCOPE, SESSION.id, read_timeout_s=120, persistence=successor
        )

        async def reconcile() -> None:
            with pytest.raises(TurnConnectionLost):
                await io.send(inputs)
            assert [event.id for event in await io.replay()] == ["accepted-message"]

        await successor.run(reconcile)
    replayed.assert_consumed()
    assert [request.method for request in transport.requests + replayed.requests] == ["POST", "GET"]
    assert len(await store.read_events(SESSION.id)) == 1
    assert (await store.get_operation(SCOPE, "root:send:0")) == record


async def test_cancel_ack_cannot_complete_the_journal_but_independent_stop_can(
    stores: Callable[[], StateStore],
) -> None:
    from daimon.core.errors import TurnError

    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
        ScriptedReply.stream("/v1/sessions/session/events/stream", []),
    )
    first = context(store)
    async with transport.client() as client:
        io = default_mux_turn_io(client, SCOPE, SESSION.id, read_timeout_s=120, persistence=first)
        with pytest.raises(TurnError, match="without terminal idle"):
            await first.run(lambda: io.interrupt(timeout_s=1))
    transport.assert_consumed()
    record = await store.get_operation(SCOPE, "root:cancel:0")
    assert record is not None and record.operation.status == "accepted"
    assert not await store.read_events(SESSION.id)
    observed = ScriptedTransport()
    observed.queue(
        ScriptedReply.stream(
            "/v1/sessions/session/events/stream",
            [make_status_idle(event_id="stopped").model_dump(mode="json")],
        )
    )
    second = context(stores())
    async with observed.client() as client:
        io = default_mux_turn_io(client, SCOPE, SESSION.id, read_timeout_s=120, persistence=second)
        stopped = await second.run(lambda: io.interrupt(timeout_s=1))
    assert stopped.stopped
    observed.assert_consumed()
    assert [request.method for request in observed.requests] == ["GET"]
    entries = await store.read_events(SESSION.id)
    assert len(entries) == 1 and entries[0].type == "session.turn_ended"
    completed = await store.get_operation(SCOPE, "root:cancel:0")
    assert completed is not None and completed.operation.status == "processed"


async def test_archive_claim_restores_receipt_without_repeating_actual_sdk_mutation(
    stores: Callable[[], StateStore],
) -> None:
    from daimon.testing.ma import session_response

    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST", "/v1/sessions/session/archive", session_response(session_id=SESSION.id)
        )
    )
    for persistence in (context(store), context(stores())):
        async with transport.client() as client:
            io = default_mux_turn_io(
                client, SCOPE, SESSION.id, read_timeout_s=120, persistence=persistence
            )
            await persistence.run(io.archive)
    transport.assert_consumed()
    assert len(transport.requests) == 1
    operation = await store.get_operation(SCOPE, "root:archive:0")
    assert operation is not None and operation.operation.status == "processed"


async def test_explicit_sdk_refusal_allows_one_fresh_attempt_with_the_same_wire_bytes(
    stores: Callable[[], StateStore],
) -> None:
    import httpx
    from anthropic import BadRequestError

    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST",
            "/v1/sessions/session/events",
            httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "refused"},
                },
            ),
        ),
        ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
    )
    persistence = context(store)
    async with transport.client() as client:
        io = default_mux_turn_io(
            client, SCOPE, SESSION.id, read_timeout_s=120, persistence=persistence
        )

        async def pump() -> None:
            with pytest.raises(BadRequestError):
                await io.send(
                    ({"type": "user.message", "content": [{"type": "text", "text": "question"}]},)
                )
            await io.send(
                ({"type": "user.message", "content": [{"type": "text", "text": "question"}]},)
            )

        await persistence.run(pump)
    transport.assert_consumed()
    assert len(transport.requests) == 2 and transport.requests[0] == transport.requests[1]
    rejected = await store.get_operation(SCOPE, "root:send:0")
    accepted = await store.get_operation(SCOPE, "root:send:1")
    assert rejected is not None and rejected.operation.status == "failed"
    assert accepted is not None and accepted.operation.status == "accepted"


async def test_main_confirmation_recovery_keeps_legacy_wire_and_the_mux_fence(
    stores: Callable[[], StateStore],
) -> None:
    import httpx
    from daimon.core.turn.termination import TerminationReason
    from daimon.testing.ma import session_response

    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    refusal = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "waiting on responses to events [pending]",
        },
    }
    idle = make_status_idle(event_id="settled").model_dump(mode="json")
    answer = make_agent_message(event_id="answer", text="recovered").model_dump(mode="json")
    results: list[tuple[object, ...]] = []
    for path in ("legacy", "mux"):
        transport = ScriptedTransport()
        transport.queue(
            ScriptedReply.stream("/v1/sessions/session/events/stream", []),
            ScriptedReply("POST", "/v1/sessions/session/events", httpx.Response(400, json=refusal)),
            ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
            ScriptedReply(
                "GET", "/v1/sessions/session", session_response(session_id="session", status="idle")
            ),
            ScriptedReply("GET", "/v1/sessions/session/events", list_response([idle])),
            ScriptedReply.stream("/v1/sessions/session/events/stream", [answer, idle]),
            ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
        )
        lifecycle = RecordingLifecycle()
        async with transport.client() as client:
            with context(store).activate():
                state = await run_turn(
                    anthropic=client,
                    session_id="session",
                    user_message="retry me",
                    lifecycle=lifecycle,
                    cancel=asyncio.Event(),
                    billing=BillingExempt(reason="headless-unrecorded"),
                    path=path,
                    scope=SCOPE,
                )
        transport.assert_consumed()
        assert state.error is None and state.termination == TerminationReason.COMPLETED
        results.append(
            (
                transport.requests,
                state.content,
                [event.model_dump(mode="json") for event in lifecycle.sse_events],
            )
        )
    assert results[0] == results[1]
    rejected = await store.get_operation(SCOPE, "root:send:0")
    interrupt = await store.get_operation(SCOPE, "root:recovery-interrupt:0")
    retried = await store.get_operation(SCOPE, "root:send:1")
    assert rejected is not None and rejected.operation.status == "failed"
    assert interrupt is not None and interrupt.operation.status == "accepted"
    assert retried is not None and retried.operation.status == "accepted"
    entries = await store.read_events(SESSION.id)
    assert [event.native.event_id for event in entries] == ["answer", "settled"]
    assert entries[-1].type == "session.turn_ended"


@pytest.mark.parametrize("change", ("order", "decision"))
async def test_cached_action_batch_refuses_changed_wire_order_or_decision(
    stores: Callable[[], StateStore],
    change: str,
) -> None:
    store = stores()
    await store.put_binding(BINDING, expected_generation=0)
    transport = ScriptedTransport()
    transport.queue(ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()))
    original: tuple[BetaManagedAgentsEventParams, ...] = (
        {"type": "user.tool_confirmation", "tool_use_id": "a", "result": "allow"},
        {"type": "user.tool_confirmation", "tool_use_id": "b", "result": "allow"},
    )
    changed: tuple[BetaManagedAgentsEventParams, ...] = (
        tuple(reversed(original))
        if change == "order"
        else (
            {"type": "user.tool_confirmation", "tool_use_id": "a", "result": "deny"},
            {"type": "user.tool_confirmation", "tool_use_id": "b", "result": "allow"},
        )
    )
    first = context(store)
    async with transport.client() as client:
        io = default_mux_turn_io(client, SCOPE, SESSION.id, read_timeout_s=120, persistence=first)
        await first.run(lambda: io.send(original))
    second = context(stores())
    async with transport.client() as client:
        io = default_mux_turn_io(client, SCOPE, SESSION.id, read_timeout_s=120, persistence=second)
        with pytest.raises(OperationConflict):
            await second.run(lambda: io.send(changed))
    transport.assert_consumed()
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "kind",
    ["send", "actions", "cancel", "archive", "recovery-send", "recovery-stop", "recovery-archive"],
)
async def test_production_retries_cannot_duplicate_a_claimed_native_mutation(kind: str) -> None:
    import httpx
    from anthropic import APIConnectionError
    from daimon.core.errors import TurnError
    from daimon.core.turn.io import TrackedLegacyTurnIO, TurnConnectionLost
    from daimon.testing.ma import session_response

    store = MemoryStateStore()
    await store.put_binding(BINDING, expected_generation=0)
    persistence = context(store)
    archive = kind in {"archive", "recovery-archive"}
    path = "/v1/sessions/session/archive" if archive else "/v1/sessions/session/events"
    reply = session_response(session_id=SESSION.id) if archive else send_events_response()
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("POST", path, httpx.ReadError("provider accepted; acknowledgement lost")),
        ScriptedReply("POST", path, reply),
    )
    inputs: tuple[BetaManagedAgentsEventParams, ...] = (
        {"type": "user.message", "content": [{"type": "text", "text": "question"}]},
    )
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        assert client.max_retries == 8 == MA_MAX_RETRIES
        if kind.startswith("recovery-"):
            io = TrackedLegacyTurnIO(client, SESSION.id, scope=SCOPE, persistence=persistence)
        else:
            io = default_mux_turn_io(
                client, SCOPE, SESSION.id, read_timeout_s=120, persistence=persistence
            )
        if kind == "actions":
            inputs = (
                {"type": "user.tool_confirmation", "tool_use_id": "action", "result": "allow"},
            )

        async def pump() -> None:
            if archive:
                await io.archive()
            elif kind in {"cancel", "recovery-stop"}:
                await io.interrupt(timeout_s=0)
            else:
                await io.send(inputs)

        with pytest.raises((TurnConnectionLost, APIConnectionError, TurnError)):
            await persistence.run(pump)
        assert client.max_retries == MA_MAX_RETRIES
    assert not transport.violations
    assert len(transport.requests) == 1 and transport.requests[0].method == "POST"
    assert len(transport.replies) == 1
    operation_kind = "send" if kind == "recovery-send" else kind
    if kind == "recovery-archive":
        operation_kind = "archive"
    if kind == "actions":
        from daimon.core.turn.io import _send_kind, neutral_inputs

        operation_kind = _send_kind(neutral_inputs(inputs))
    record = await store.get_operation(SCOPE, f"root:{operation_kind}:0")
    assert record is not None and record.operation.status == "outcome_unknown"


async def test_a_claim_keeps_the_default_retry_policy_for_reads_and_legacy_mutations() -> None:
    import httpx
    from daimon.core.turn.io import LegacyTurnIO
    from daimon.testing.ma import session_response

    store = MemoryStateStore()
    await store.put_binding(BINDING, expected_generation=0)
    persistence = context(store)
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("GET", "/v1/sessions/session", httpx.ReadError("retryable read")),
        ScriptedReply(
            "GET", "/v1/sessions/session", session_response(session_id=SESSION.id, status="idle")
        ),
        ScriptedReply("POST", "/v1/sessions/session/events", httpx.ReadError("legacy retry")),
        ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
    )
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        io = default_mux_turn_io(
            client, SCOPE, SESSION.id, read_timeout_s=120, persistence=persistence
        )

        async def read(key: str) -> SendReceipt:
            assert await io.status() == "idle"
            return SendReceipt(operation_id=key, status="processed", input_ids=())

        await persistence.run(
            lambda: persistence.mutate(
                SESSION, "read-proof", {}, read, SendReceipt, lambda receipt: "processed"
            )
        )
        legacy = LegacyTurnIO(client, SESSION.id, scope=SCOPE)
        await legacy.send(
            ({"type": "user.message", "content": [{"type": "text", "text": "legacy"}]},)
        )
        assert client.max_retries == MA_MAX_RETRIES
    transport.assert_consumed()
    assert [request.method for request in transport.requests] == ["GET", "GET", "POST", "POST"]
