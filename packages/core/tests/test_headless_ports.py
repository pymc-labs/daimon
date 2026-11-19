"""Headless resource assembly retains the exact direct SDK requests."""

import uuid

import httpx
import pytest
from daimon.core import headless_runner
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.state import TurnState
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.errors import ScopeViolation
from mux.testing.drivers.anthropic.ma import MARouter
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

_ACCOUNT_ID = uuid.UUID(int=9)


@pytest.mark.parametrize("tenant_id", [None, uuid.UUID(int=8)])
async def test_headless_resource_requests_match_direct_sdk(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID | None
) -> None:
    """The agent and environment are retrieved with the same requests as the
    SDK called directly. A turn never runs without its tenant: the tenantless
    case is refused before any provider request.
    """
    agent = ma_agent(id="agent-headless", tenant_id=tenant_id)
    environment = ma_environment(id="env-headless", tenant_id=tenant_id)

    def transport() -> ScriptedTransport:
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
        return ScriptedTransport(router=router)

    before, after = transport(), transport()
    async with before.client() as client:
        await client.beta.agents.retrieve(agent.id)
        await client.beta.environments.retrieve(environment.id)

    async def create_session(client, *, agent, environment, **kwargs):
        assert agent.model_dump(mode="json") == original_agent.model_dump(mode="json")
        assert environment.model_dump(mode="json") == original_environment.model_dump(mode="json")
        assert kwargs["tenant_id"] == tenant_id
        return ma_session()

    driven: list[object] = []

    async def drive_turn(**kwargs):
        driven.append(kwargs)
        return TurnState()

    original_agent, original_environment = agent, environment
    monkeypatch.setattr(headless_runner, "create_session", create_session)
    monkeypatch.setattr(headless_runner, "drive_turn", drive_turn)
    if tenant_id is None:
        async with after.client() as client:
            with pytest.raises(ScopeViolation, match="require the authorized tenant/account"):
                await headless_runner.run_turn(
                    tool_safety=OPEN_TOOL_SAFETY,
                    anthropic=client,
                    agent_id=agent.id,
                    environment_id=environment.id,
                    tenant_id=tenant_id,
                    account_id=_ACCOUNT_ID,
                    trigger_message="headless",
                )
        assert after.requests == [], "a refused turn must not call the provider"
        assert driven == [], "a refused turn must not reach the driver"
        return
    async with after.client() as client:
        assert (
            await headless_runner.run_turn(
                anthropic=client,
                agent_id=agent.id,
                environment_id=environment.id,
                tenant_id=tenant_id,
                account_id=_ACCOUNT_ID,
                trigger_message="headless",
                tool_safety=OPEN_TOOL_SAFETY,
            )
            == ""
        )
    assert len(driven) == 1
    before.assert_consumed()
    after.assert_consumed()
    assert after.requests == before.requests
    assert [(request.method, request.path) for request in after.requests] == [
        ("GET", "/v1/agents/agent-headless"),
        ("GET", "/v1/environments/env-headless"),
    ]
