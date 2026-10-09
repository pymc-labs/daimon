"""Contract fakes exercise the bridge independently of N5's lifecycle driver."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from daimon.core.turn.io import MuxTurnIO, TurnConnectionLost
from mux.contracts.actions import UserMessage
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import SendReceipt
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
async def test_status_uses_owned_sessions_contract_and_original_scope(state, legacy_status):
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
async def test_send_acceptance_and_unknown_receipt_never_resend(status):
    send = AsyncMock(
        return_value=SendReceipt(operation_id="operation", status=status, input_ids=())
    )
    backend = cast(ManagedAgents, SimpleNamespace(events=SimpleNamespace(send=send)))
    io = MuxTurnIO(backend, SCOPE, SESSION)
    batch = [{"type": "user.message", "content": [{"type": "text", "text": "question"}]}]
    if status == "outcome_unknown":
        with pytest.raises(TurnConnectionLost):
            await io.send(batch)
    else:
        await io.send(batch)
    send.assert_awaited_once()
    received_scope, received_ref, inputs = send.await_args.args
    assert received_scope is SCOPE and received_ref is SESSION
    assert len(inputs) == 1 and isinstance(inputs[0], UserMessage)
    assert inputs[0].content[0].text == "question"
    assert send.await_args.kwargs["key"]


async def test_neutral_network_error_reaches_host_reconnect_without_sdk_exception():
    send = AsyncMock(side_effect=ProviderError("transient_network", retryable=True))
    backend = cast(ManagedAgents, SimpleNamespace(events=SimpleNamespace(send=send)))
    with pytest.raises(TurnConnectionLost) as caught:
        await MuxTurnIO(backend, SCOPE, SESSION).send([])
    assert isinstance(caught.value.__cause__, ProviderError)
    send.assert_awaited_once()
