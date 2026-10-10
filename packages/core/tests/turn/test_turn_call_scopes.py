"""Admitted identity reaches the private transport without changing SDK traffic."""

import asyncio
from types import SimpleNamespace
from typing import Literal, cast
from uuid import UUID

import pytest
from anthropic import AsyncAnthropic
from daimon.core import ma
from daimon.core.errors import TurnError
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.io import turn_io
from daimon.core.turn.run import _turn_port_kwargs  # pyright: ignore[reportPrivateUsage]
from daimon.testing.ma import list_response, send_events_response
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from mux.drivers.anthropic.transport import LegacyTurnTransport
from mux.errors import ScopeViolation

from .conftest import make_status_idle

TENANT = UUID(int=601)
ACCOUNT = UUID(int=602)
SCOPE = Scope(
    tenant_id=str(TENANT),
    account_id=str(ACCOUNT),
    principal_id="daimon",
    authorization_id="admitted-turn",
)


@pytest.mark.parametrize("path", ["legacy", "mux"])
def test_prepared_turn_binds_the_real_account_on_both_paths(
    monkeypatch: pytest.MonkeyPatch, path: Literal["legacy", "mux"]
) -> None:
    monkeypatch.setenv("DAIMON_TURN__PATH", "legacy")
    admission = cast(
        Admission, SimpleNamespace(account_id=ACCOUNT, grant=None, backend_revision=None)
    )
    deps = cast(TurnDeps, SimpleNamespace(turn_path=path, backend=None))
    args = _turn_port_kwargs(deps, admission, "session", tenant_id=TENANT)
    assert args.get("scope") == SCOPE
    assert "backend" not in args and "session_ref" not in args
    if path == "legacy":
        assert "path" not in args
    else:
        assert args.get("path") == "mux"


@pytest.mark.parametrize("path", ["legacy", "mux"])
def test_prepared_turn_refuses_a_grant_from_another_tenant(
    path: Literal["legacy", "mux"],
) -> None:
    grant = cast(AdmissionGrant, SimpleNamespace(tenant_id=UUID(int=603)))
    admission = cast(Admission, SimpleNamespace(account_id=ACCOUNT, grant=grant))
    deps = cast(TurnDeps, SimpleNamespace(turn_path=path, backend=None))
    with pytest.raises(ScopeViolation):
        _turn_port_kwargs(deps, admission, "session", tenant_id=TENANT)


async def test_legacy_replay_and_interrupt_forward_scope_and_match_original_sdk_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    idle = make_status_idle(event_id="idle")
    scopes: list[Scope | None] = []

    class CapturingTransport(LegacyTurnTransport):
        def __init__(self, client: AsyncAnthropic, session_id: str, *, scope: Scope | None = None):
            scopes.append(scope)
            super().__init__(client, session_id, scope=scope)

    monkeypatch.setattr(ma, "LegacyTurnTransport", CapturingTransport)
    results: list[object] = []
    for migrated in (False, True):
        transport = ScriptedTransport()
        transport.queue(
            ScriptedReply(
                "GET", "/v1/sessions/session/events", list_response([idle.model_dump(mode="json")])
            ),
            ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
            ScriptedReply.stream(
                "/v1/sessions/session/events/stream", [idle.model_dump(mode="json")]
            ),
        )
        async with transport.client() as client:
            if migrated:
                io = turn_io(client, "session", path="legacy", scope=SCOPE)
                events = await io.replay(timeout_s=1)
                await io.interrupt(timeout_s=1)
            else:
                events = [
                    event async for event in client.beta.sessions.events.list(session_id="session")
                ]
                await client.beta.sessions.events.send(
                    "session", events=[{"type": "user.interrupt"}]
                )
                stream = await client.beta.sessions.events.stream(session_id="session")
                async for event in stream:
                    if event.type == "session.status_idle":
                        break
                await stream.close()
        transport.assert_consumed()
        results.append(
            (
                [request.to_dict() for request in transport.requests],
                [request.body for request in transport.requests],
                events,
            )
        )
    assert results[0] == results[1]
    assert scopes == [SCOPE, SCOPE]


@pytest.mark.parametrize(
    "scope",
    [Scope.platform(reason="operator"), Scope.legacy_host_authorized(call_site="temporary")],
)
async def test_migrated_turn_helpers_refuse_privileged_scope_before_io(scope: Scope) -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(ScopeViolation):
            await ma.replay_events(client, session_id="session", scope=scope)
        with pytest.raises(ScopeViolation):
            await ma.send_interrupt_and_wait(client, session_id="session", scope=scope)
    assert transport.requests == []


async def test_interrupt_timeout_keeps_its_original_error_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Scope | None] = []

    class StalledTransport(LegacyTurnTransport):
        async def open_interrupt_stream(self):
            seen.append(self.scope)
            await asyncio.sleep(10)
            return await super().open_interrupt_stream()

    monkeypatch.setattr(ma, "LegacyTurnTransport", StalledTransport)
    transport = ScriptedTransport()
    transport.queue(ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()))
    async with transport.client() as client:
        with pytest.raises(TurnError) as error:
            await ma.send_interrupt_and_wait(
                client, session_id="session", timeout_s=0.01, scope=SCOPE
            )
    assert error.value.kind == "interrupt_timeout"
    assert isinstance(error.value.__cause__, TimeoutError)
    assert seen == [SCOPE]
    transport.assert_consumed()
