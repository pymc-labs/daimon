"""Headless resource assembly retains the exact legacy SDK requests."""

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

from .turn_path_support import TurnPath

_ACCOUNT_ID = uuid.UUID(int=9)


@pytest.mark.parametrize("tenant_id", [None, uuid.UUID(int=8)])
async def test_headless_resource_requests_match_direct_sdk(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID | None, turn_path: TurnPath
) -> None:
    """Both paths retrieve the agent and environment with the same requests as
    the SDK called directly, and hand the driver the path they were given.

    A mux turn never runs without its tenant: the tenantless case is the
    legacy host's, so on mux it must be refused before any provider request.
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

    driven_paths: list[str] = []

    async def drive_turn(**kwargs):
        driven_paths.append(kwargs["path"])
        return TurnState()

    original_agent, original_environment = agent, environment
    monkeypatch.setattr(headless_runner, "create_session", create_session)
    monkeypatch.setattr(headless_runner, "drive_turn", drive_turn)
    if tenant_id is None and turn_path == "mux":
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
                    turn_path=turn_path,
                )
        assert after.requests == [], "a refused turn must not call the provider"
        assert driven_paths == [], "a refused turn must not reach the driver"
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
                turn_path=turn_path,
            )
            == ""
        )
    assert driven_paths == [turn_path], (
        "the driver is handed the injected path, not a settings read"
    )
    before.assert_consumed()
    after.assert_consumed()
    assert after.requests == before.requests
    assert [(request.method, request.path) for request in after.requests] == [
        ("GET", "/v1/agents/agent-headless"),
        ("GET", "/v1/environments/env-headless"),
    ]
