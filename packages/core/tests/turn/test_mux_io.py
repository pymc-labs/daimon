"""Contract fakes exercise the bridge independently of N5's lifecycle driver."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsEventParams
from daimon.core.errors import TurnError
from daimon.core.turn.driver import (
    _handle_interrupt_in_consume,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.turn.io import MuxTurnIO, TurnConnectionLost, TurnIO
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason, stop_termination_reason
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.actions import UserMessage
from mux.contracts.events import Event, TextPart, TurnOutcome
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import Session
from mux.drivers.anthropic.normalize import EventNormalizer
from mux.errors import ProviderError

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="admitted-turn"
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="anthropic",
    account_scope_id="workspace",
    tenant_id=SCOPE.tenant_id,
    account_id=SCOPE.account_id,
)


@pytest.mark.parametrize(
    "state,legacy_status",
    [
        ("provisioning", "rescheduling"),
        ("idle", "idle"),
        ("running", "running"),
        ("requires_action", "idle"),
        ("terminated", "terminated"),
    ],
)
async def test_status_uses_owned_sessions_contract_and_original_scope(
    state: Literal["provisioning", "idle", "running", "requires_action", "terminated"],
    legacy_status: str,
) -> None:
    owned = Session.model_validate(
        {
            "ref": SESSION,
            "binding": {
                "id": "binding",
                "thread": {
                    "channel": {"tenant_id": "tenant", "platform": "test", "channel_id": "channel"},
                    "thread_id": "thread",
                },
                "provider": "anthropic",
                "profile": "anthropic.managed_agents",
                "native_refs": {"session": "session"},
                "generation": 0,
                "config_revision": 0,
            },
            "continuity": {
                "conversation": "native_session",
                "workspace": "native_reuse",
                "processes": "live",
            },
            "requested_revision": {"local": 0},
            "effective_revision": {"local": 0},
            "state": state,
        }
    )
    retrieve = AsyncMock(return_value=owned)
    backend = cast(ManagedAgents, SimpleNamespace(sessions=SimpleNamespace(retrieve=retrieve)))
    status = await MuxTurnIO(backend, SCOPE, SESSION).status()
    assert status == legacy_status
    retrieve.assert_awaited_once_with(SCOPE, SESSION)


@pytest.mark.parametrize("status", ["processed", "queued", "outcome_unknown"])
async def test_send_acceptance_and_unknown_receipt_never_resend(
    status: Literal["processed", "queued", "outcome_unknown"],
) -> None:
    send = AsyncMock(
        return_value=SendReceipt(operation_id="operation", status=status, input_ids=())
    )
    backend = cast(ManagedAgents, SimpleNamespace(events=SimpleNamespace(send=send)))
    io = MuxTurnIO(backend, SCOPE, SESSION)
    batch: list[BetaManagedAgentsEventParams] = [
        {"type": "user.message", "content": [{"type": "text", "text": "question"}]}
    ]
    if status == "outcome_unknown":
        with pytest.raises(TurnConnectionLost):
            await io.send(batch)
    else:
        await io.send(batch)
    send.assert_awaited_once()
    assert send.await_args is not None
    received_scope, received_ref, inputs = send.await_args.args
    assert received_scope is SCOPE and received_ref is SESSION
    assert len(inputs) == 1 and isinstance(inputs[0], UserMessage)
    assert isinstance(inputs[0].content[0], TextPart)
    assert inputs[0].content[0].text == "question"
    assert send.await_args.kwargs["key"]


async def test_neutral_network_error_reaches_host_reconnect_without_sdk_exception():
    send = AsyncMock(side_effect=ProviderError("transient_network", retryable=True))
    backend = cast(ManagedAgents, SimpleNamespace(events=SimpleNamespace(send=send)))
    with pytest.raises(TurnConnectionLost) as caught:
        await MuxTurnIO(backend, SCOPE, SESSION).send([])
    assert isinstance(caught.value.__cause__, ProviderError)
    send.assert_awaited_once()


@pytest.mark.parametrize("stopped", [False, True])
async def test_interrupt_requires_an_observation_and_preserves_receipt_correlation(
    stopped: bool,
) -> None:
    now = datetime.now(UTC)
    requested = CancelReceipt(
        operation_id="cancel-operation",
        session=SESSION,
        turn_id="root",
        status="requested",
        requested_at=now,
    )
    observed = StopObservation(
        receipt_operation_id=requested.operation_id,
        stopped=stopped,
        outcome="interrupted" if stopped else None,
        observed_at=now,
    )
    cancel = AsyncMock(return_value=requested)
    wait = AsyncMock(return_value=observed)
    backend = cast(
        ManagedAgents, SimpleNamespace(events=SimpleNamespace(cancel=cancel, wait_stopped=wait))
    )
    io = MuxTurnIO(backend, SCOPE, SESSION)
    if stopped:
        assert await io.interrupt(timeout_s=1) is observed
    else:
        with pytest.raises(TurnError) as caught:
            await io.interrupt(timeout_s=1)
        assert caught.value.kind == "interrupt_timeout"
    cancel.assert_awaited_once()
    wait.assert_awaited_once()
    assert wait.await_args is not None
    assert wait.await_args.args == (SCOPE, requested)
    assert wait.await_args.kwargs["deadline"] > requested.requested_at


async def test_stop_observation_for_another_cancel_is_rejected():
    now = datetime.now(UTC)
    requested = CancelReceipt(
        operation_id="cancel-operation",
        session=SESSION,
        turn_id="root",
        status="requested",
        requested_at=now,
    )
    observed = StopObservation(
        receipt_operation_id="other-operation", stopped=True, outcome="interrupted", observed_at=now
    )
    backend = cast(
        ManagedAgents,
        SimpleNamespace(
            events=SimpleNamespace(
                cancel=AsyncMock(return_value=requested),
                wait_stopped=AsyncMock(return_value=observed),
            )
        ),
    )
    with pytest.raises(ProviderError) as caught:
        await MuxTurnIO(backend, SCOPE, SESSION).interrupt(timeout_s=1)
    assert caught.value.native_code == "foreign_stop_observation"


async def test_cancel_receipt_for_another_session_never_opens_its_wait_stream():
    requested = CancelReceipt(
        operation_id="cancel-operation",
        session=SESSION.model_copy(update={"id": "other"}),
        turn_id="root",
        status="requested",
        requested_at=datetime.now(UTC),
    )
    wait = AsyncMock()
    backend = cast(
        ManagedAgents,
        SimpleNamespace(
            events=SimpleNamespace(cancel=AsyncMock(return_value=requested), wait_stopped=wait)
        ),
    )
    with pytest.raises(ProviderError) as caught:
        await MuxTurnIO(backend, SCOPE, SESSION).interrupt(timeout_s=1)
    assert caught.value.native_code == "foreign_cancel_receipt"
    wait.assert_not_awaited()


@pytest.mark.parametrize(
    "outcome,reason",
    [
        ("completed", TerminationReason.COMPLETED),
        ("interrupted", TerminationReason.INTERRUPTED),
        ("errored", TerminationReason.UPSTREAM),
        ("terminated", TerminationReason.SESSION_TERMINATED),
        (None, TerminationReason.UNKNOWN),
    ],
)
async def test_interrupt_terminal_hook_uses_observed_outcome_instead_of_cancel_intent(
    outcome: TurnOutcome | None, reason: TerminationReason
) -> None:
    observed = StopObservation(
        receipt_operation_id="cancel", stopped=True, outcome=outcome, observed_at=datetime.now(UTC)
    )
    assert stop_termination_reason(observed) == reason
    io = cast(TurnIO, SimpleNamespace(interrupt=AsyncMock(return_value=observed)))
    lifecycle = RecordingLifecycle()
    render = AsyncMock()
    state = await _handle_interrupt_in_consume(
        io=io,
        session_id="session",
        state_cell=[TurnState()],
        lifecycle=lifecycle,
        render_once=render,
        interrupt_timeout_s=1,
        renders_failed=0,
    )
    assert state.termination == reason
    assert len(lifecycle.terminal_success) == int(not reason.is_failure)
    assert len(lifecycle.terminal_failures) == int(reason.is_failure)
    render.assert_awaited_once()


def test_unobserved_stop_never_claims_interrupted_even_if_the_outcome_field_says_so():
    observation = StopObservation(
        receipt_operation_id="cancel",
        stopped=False,
        outcome="interrupted",
        observed_at=datetime.now(UTC),
    )
    assert stop_termination_reason(observation) == TerminationReason.INTERRUPT_TIMEOUT


async def test_archive_uses_the_bound_session_and_caller_scope():
    archive = AsyncMock(return_value=object())
    backend = cast(ManagedAgents, SimpleNamespace(sessions=SimpleNamespace(archive=archive)))
    await MuxTurnIO(backend, SCOPE, SESSION).archive()
    archive.assert_awaited_once()
    assert archive.await_args is not None
    assert archive.await_args.args == (SCOPE, SESSION)
    assert archive.await_args.kwargs["key"]


@pytest.mark.parametrize(
    "changed", [{"kind": "agent"}, {"tenant_id": "another"}, {"account_id": "another"}]
)
def test_foreign_session_binding_fails_before_any_port_io(changed: dict[str, str]) -> None:
    from mux.errors import ScopeViolation

    backend = cast(ManagedAgents, SimpleNamespace())
    with pytest.raises(ScopeViolation):
        MuxTurnIO(backend, SCOPE, SESSION.model_copy(update=changed))


class _History:
    """An Anthropic session log behind the mux ports: send and the full walk."""

    def __init__(self, *records: dict[str, object], lands: bool = True, ack: str = "processed"):
        self.records = list(records)
        self.lands, self.ack = lands, ack
        self.sent = 0

    def events(self) -> list[Event]:
        normalizer = EventNormalizer(SESSION)
        now = datetime.now(UTC)
        return [normalizer.normalize(dict(record), observed_at=now) for record in self.records]

    async def send(self, scope: Scope, session: ResourceRef, inputs: object, *, key: str):
        self.sent += 1
        input_id = f"user-{len(self.records)}"
        if self.lands:
            self.records.append(_user(input_id, "question"))
        ids = (input_id,) if self.ack == "processed" else ()
        return SendReceipt(operation_id=key, status=self.ack, input_ids=ids)  # type: ignore[arg-type]

    async def walk(self, scope: Scope, session: ResourceRef) -> AsyncIterator[Event]:
        for event in self.events():
            yield event

    def backend(self) -> ManagedAgents:
        return cast(
            ManagedAgents,
            SimpleNamespace(
                events=SimpleNamespace(send=self.send),
                extension=lambda port, *, namespace, version: self,
            ),
        )


class _Journal:
    """The host journal: what earlier turns recorded, deduplicated by native id."""

    def __init__(self, *events: Event) -> None:
        self.rows: list[Event] = []
        for event in events:
            self.add(event)
        self.store = SimpleNamespace(read_events=self.read_events)

    def add(self, event: Event) -> None:
        if all(row.native.event_id != event.native.event_id for row in self.rows):
            self.rows.append(event.model_copy(update={"sequence": len(self.rows)}))

    async def read_events(self, session_id: str, *, after: int = -1, limit: int = 1000):
        assert session_id == SESSION.id
        return self.rows[after + 1 : after + 1 + limit]

    def check_session(self, scope: Scope, session: ResourceRef) -> None:
        assert session == SESSION

    async def record(self, session: ResourceRef, event: Event) -> None:
        self.add(event)

    async def mutate(self, session, kind, request, call, response, status):
        return await call("operation-key")


def _record(id_: str, type_: str, **values: object) -> dict[str, object]:
    return {"id": id_, "type": type_, "processed_at": "2026-10-29T00:00:00+00:00", **values}


def _user(id_: str, text: str) -> dict[str, object]:
    return _record(id_, "user.message", content=[{"type": "text", "text": text}])


# The previous turn asked the same words and finished; the host journaled it.
_PREVIOUS = (
    _user("user-0", "question"),
    _record("agent-1", "agent.message", content=[{"type": "text", "text": "old answer"}]),
    _record("idle-2", "session.status_idle", stop_reason={"type": "end_turn"}),
)
_QUESTION: list[BetaManagedAgentsEventParams] = [
    {"type": "user.message", "content": [{"type": "text", "text": "question"}]}
]


def _io(history: _History) -> tuple[MuxTurnIO, _Journal]:
    journal = _Journal(*history.events())
    io = MuxTurnIO(history.backend(), SCOPE, SESSION, persistence=cast(TurnPersistence, journal))
    return io, journal


async def test_lost_acknowledgement_reattaches_when_the_input_landed() -> None:
    # F12: the designed reattach still works; the input is new to the journal.
    history = _History(*_PREVIOUS, ack="outcome_unknown")
    io, _ = _io(history)
    with pytest.raises(TurnConnectionLost):
        await io.send(_QUESTION)
    replayed = await io.replay()
    assert history.sent == 1 and [e.id for e in replayed][-1] == "user-3"


async def test_lost_acknowledgement_never_adopts_the_previous_answer() -> None:
    # F12: the input never reached the session. The earlier turn's ending must
    # not read as this turn's answer, even with the same words, on any replay.
    history = _History(*_PREVIOUS, ack="outcome_unknown", lands=False)
    io, _ = _io(history)
    with pytest.raises(TurnConnectionLost):
        await io.send(_QUESTION)
    for _ in range(2):
        with pytest.raises(TurnConnectionLost, match="did not reach the session"):
            await io.replay()
    assert history.sent == 1


async def test_an_input_the_live_stream_already_journaled_is_this_turns() -> None:
    history = _History(*_PREVIOUS, ack="outcome_unknown")
    io, journal = _io(history)
    with pytest.raises(TurnConnectionLost):
        await io.send(_QUESTION)
    # The stream delivered the input and journaled it before it dropped.
    await io._record(history.events()[-1])  # pyright: ignore[reportPrivateUsage]
    assert journal.rows[-1].id == "user-3"
    assert [e.id for e in await io.replay()][-1] == "user-3"


async def test_a_confirmed_input_must_be_in_the_replayed_history() -> None:
    history = _History(*_PREVIOUS)
    io, _ = _io(history)
    await io.send(_QUESTION)
    await io.replay()
    history.records.pop()  # a history that lost the receipt's own input
    with pytest.raises(TurnConnectionLost, match="not in the session history"):
        await io.replay()


async def test_a_turn_that_never_posted_replays_unchecked() -> None:
    # A restored receipt (another process posted) has nothing to compare against.
    history = _History(*_PREVIOUS)
    io, _ = _io(history)
    assert [e.id for e in await io.replay()][-1] == "idle-2"
