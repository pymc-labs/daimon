"""Real SDK headless turns require the caller's actual mux authorization."""

import uuid
from typing import Literal

import httpx
import pytest
from daimon.core import headless_runner
from daimon.core.mux_backend import resource_scope
from daimon.testing.ma import MARouter, sse_response
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation


@pytest.mark.parametrize("path", ["legacy", "mux"])
async def test_headless_turn_uses_authorized_scope_and_original_requests(
    monkeypatch: pytest.MonkeyPatch, path: Literal["legacy", "mux"]
) -> None:
    monkeypatch.setenv("DAIMON_TURN__PATH", path)
    tenant_id, account_id = uuid.UUID(int=23), uuid.UUID(int=24)
    scopes: list[Scope] = []

    def capture_scope(
        *, tenant_id: str, account_id: str = "service", authorization_id: str = "host-resource"
    ) -> Scope:
        scope = resource_scope(
            tenant_id=tenant_id, account_id=account_id, authorization_id=authorization_id
        )
        scopes.append(scope)
        return scope

    monkeypatch.setattr(headless_runner, "resource_scope", capture_scope)
    agent = ma_agent(id="agent-headless", tenant_id=tenant_id)
    environment = ma_environment(id="env-headless", tenant_id=tenant_id)
    session = ma_session(id="session-headless", agent_id=agent.id, environment_id=environment.id)
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/agent-headless",
        lambda request, match: httpx.Response(200, json=agent.model_dump(mode="json")),
    )
    router.add(
        "GET",
        r"/v1/environments/env-headless",
        lambda request, match: httpx.Response(200, json=environment.model_dump(mode="json")),
    )
    router.add(
        "POST",
        r"/v1/sessions$",
        lambda request, match: httpx.Response(200, json=session.model_dump(mode="json")),
    )
    router.add(
        "GET",
        r"/v1/sessions/session-headless/events/stream",
        lambda request, match: sse_response(
            [
                {
                    "id": "event-answer",
                    "type": "agent.message",
                    "processed_at": "2026-04-01T12:00:00Z",
                    "content": [{"type": "text", "text": "authorized"}],
                },
                {
                    "id": "event-idle",
                    "type": "session.status_idle",
                    "processed_at": "2026-04-01T12:00:01Z",
                    "stop_reason": {"type": "end_turn"},
                },
            ]
        ),
    )
    router.add(
        "POST",
        r"/v1/sessions/session-headless/events$",
        lambda request, match: httpx.Response(200, json={"data": None}),
    )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        assert (
            await headless_runner.run_turn(
                anthropic=client,
                agent_id=agent.id,
                environment_id=environment.id,
                trigger_message="hello",
                tenant_id=tenant_id,
                account_id=account_id,
            )
            == "authorized"
        )
    transport.assert_consumed()
    assert [(request.method, request.path) for request in transport.requests] == [
        ("GET", "/v1/agents/agent-headless"),
        ("GET", "/v1/environments/env-headless"),
        ("POST", "/v1/sessions"),
        ("GET", "/v1/sessions/session-headless/events/stream"),
        ("POST", "/v1/sessions/session-headless/events"),
    ]
    turn_scopes = [scope for scope in scopes if scope.authorization_id == "headless-turn"]
    if path == "mux":
        assert turn_scopes == [
            Scope(
                tenant_id=str(tenant_id),
                account_id=str(account_id),
                principal_id="daimon",
                authorization_id="headless-turn",
            )
        ]
        assert not turn_scopes[0].is_platform
        assert not turn_scopes[0].is_legacy_host_authorized
    else:
        assert turn_scopes == []


@pytest.mark.parametrize(
    "tenant_id,account_id",
    [
        (None, None),
        (uuid.UUID(int=23), None),
        (None, uuid.UUID(int=24)),
    ],
)
async def test_mux_headless_missing_identity_fails_before_provider_io(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID | None, account_id: uuid.UUID | None
) -> None:
    monkeypatch.setenv("DAIMON_TURN__PATH", "mux")
    transport = ScriptedTransport(router=MARouter())
    async with transport.client() as client:
        with pytest.raises(ScopeViolation, match="authorized tenant/account"):
            await headless_runner.run_turn(
                anthropic=client,
                agent_id="agent-headless",
                environment_id="env-headless",
                trigger_message="hello",
                tenant_id=tenant_id,
                account_id=account_id,
            )
    assert transport.requests == []
