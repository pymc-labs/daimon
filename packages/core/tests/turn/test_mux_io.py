"""Contract fakes exercise the bridge independently of N5's lifecycle driver."""

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
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason, stop_termination_reason
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart, TurnOutcome
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import Session
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
