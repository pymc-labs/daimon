"""Smoke tests for daimon.testing.ma module.

Validates MA handler factories, combine_handlers, build_fake_anthropic,
build_stub_anthropic, make_fake_ma_handler, MARouter, NotHandled, and
shared constants.
"""

from __future__ import annotations

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaCloudConfig, BetaEnvironment
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    EMPTY_SESSION_STATS,
    EMPTY_SESSION_USAGE,
    MARouter,
    NotHandled,
    _environment_response,
    build_fake_anthropic,
    build_stub_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
)


def test_combine_handlers_dispatches_to_matching_handler() -> None:
    """combine_handlers routes to the first handler that does not raise NotHandled."""

    def first_handler(request: httpx.Request) -> httpx.Response:
        raise NotHandled

    def second_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"from": "second"})

    combined = combine_handlers(first_handler, second_handler)
    request = httpx.Request("GET", "https://api.anthropic.com/v1/agents")
    response = combined(request)

    assert response.status_code == 200, "combine_handlers should route to the matching handler"
    assert response.json() == {"from": "second"}, "response body should come from second_handler"


def test_combine_handlers_raises_on_no_match() -> None:
    """combine_handlers raises AssertionError when no handler matches."""

    def always_unhandled(request: httpx.Request) -> httpx.Response:
        raise NotHandled

    combined = combine_handlers(always_unhandled)
    request = httpx.Request("GET", "https://api.anthropic.com/v1/agents")

    with pytest.raises(AssertionError, match="No handler matched"):
        combined(request)


def test_build_fake_anthropic_returns_async_client() -> None:
    """build_fake_anthropic with a trivial handler returns an AsyncAnthropic instance."""

    def trivial_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = build_fake_anthropic(trivial_handler)
    assert isinstance(client, AsyncAnthropic), "build_fake_anthropic should return AsyncAnthropic"


def test_build_stub_anthropic_returns_async_client() -> None:
    """build_stub_anthropic() with no args returns an AsyncAnthropic instance."""
    client = build_stub_anthropic()
    assert isinstance(client, AsyncAnthropic), "build_stub_anthropic should return AsyncAnthropic"


def test_build_stub_anthropic_with_handler() -> None:
    """build_stub_anthropic(handler=...) uses the provided handler."""
    call_log: list[str] = []

    def custom_handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request.url.path)
        return httpx.Response(200, json={"custom": True})

    client = build_stub_anthropic(handler=custom_handler)
    assert isinstance(client, AsyncAnthropic), (
        "should still return AsyncAnthropic with custom handler"
    )


def test_ma_router_dispatch_matches_route() -> None:
    """MARouter dispatches a matching request to the registered handler."""
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: httpx.Response(200, json={"matched": True}))

    request = httpx.Request("GET", "https://api.anthropic.com/v1/agents")
    response = router.dispatch(request)

    assert response.status_code == 200, "MARouter should dispatch to the matching handler"
    assert response.json() == {"matched": True}, "response should come from the registered handler"


def test_ma_router_dispatch_no_match_raises() -> None:
    """MARouter raises AssertionError when no route matches the request."""
    router = MARouter()

    request = httpx.Request("GET", "https://api.anthropic.com/v1/unknown")
    with pytest.raises(AssertionError, match="no route for"):
        router.dispatch(request)


def test_list_response_format() -> None:
    """list_response produces the MA list envelope: {data: [...], next_page: None}."""
    response = list_response([{"id": "x"}])

    assert response.status_code == 200, "list_response should return 200"
    body = response.json()
    assert body == {"data": [{"id": "x"}], "next_page": None}, (
        "list_response should wrap data in MA list envelope"
    )


def test_shared_constants_are_valid() -> None:
    """EMPTY_CLOUD_CONFIG, EMPTY_SESSION_STATS, EMPTY_SESSION_USAGE are valid SDK types."""
    assert isinstance(EMPTY_CLOUD_CONFIG, BetaCloudConfig), (
        "EMPTY_CLOUD_CONFIG should be BetaCloudConfig instance"
    )
    assert isinstance(EMPTY_SESSION_STATS, BetaManagedAgentsSessionStats), (
        "EMPTY_SESSION_STATS should be BetaManagedAgentsSessionStats instance"
    )
    assert isinstance(EMPTY_SESSION_USAGE, BetaManagedAgentsSessionUsage), (
        "EMPTY_SESSION_USAGE should be BetaManagedAgentsSessionUsage instance"
    )


async def test_make_fake_ma_handler_creates_agent() -> None:
    """make_fake_ma_handler() processes POST /v1/agents and returns a created agent."""
    handler = make_fake_ma_handler()
    client = build_fake_anthropic(handler)

    # Create an agent via the real SDK method, which hits our handler
    agent = await client.beta.agents.create(
        name="test-agent",
        model="claude-sonnet-4-6",
    )

    assert agent.id.startswith("agent_"), "created agent should have an MA-style prefixed id"
    assert agent.name == "test-agent", "created agent should have the requested name"


def test_environment_response_builder_returns_valid_sdk_type() -> None:
    """_environment_response builds a real BetaEnvironment serializable via model_dump."""
    result = _environment_response(environment_id="env_test123", name="my-env")

    assert isinstance(result, BetaEnvironment), (
        "_environment_response should return a real BetaEnvironment instance"
    )
    assert result.id == "env_test123", "environment id should match the argument"
    assert result.name == "my-env", "environment name should match the argument"
    assert result.type == "environment", "type should always be 'environment'"
    assert isinstance(result.config, BetaCloudConfig), (
        "config should be a BetaCloudConfig (EMPTY_CLOUD_CONFIG)"
    )


def test_environment_response_builder_model_dump_is_serializable() -> None:
    """_environment_response result serializes cleanly via model_dump(mode='json')."""
    env = _environment_response(environment_id="env_abc", name="test")
    dumped = env.model_dump(mode="json")

    assert dumped["id"] == "env_abc", "model_dump should preserve the id"
    assert dumped["type"] == "environment", "model_dump should preserve the type"


async def test_make_fake_ma_handler_retrieves_environment_by_id() -> None:
    """make_fake_ma_handler() answers GET /v1/environments/{id} with a 200 BetaEnvironment payload."""
    handler = make_fake_ma_handler()
    client = build_fake_anthropic(handler)

    # Retrieve an environment via the real SDK method — hits our handler
    env = await client.beta.environments.retrieve("env_test123")

    assert isinstance(env, BetaEnvironment), (
        "environments.retrieve should return a BetaEnvironment instance"
    )
    assert env.id == "env_test123", "retrieved environment id should match the requested id"
    assert env.type == "environment", "type field should always be 'environment'"


def test_ma_router_environment_retrieve_route_matches_path() -> None:
    """MARouter with a GET /v1/environments/{id} route dispatches environment-retrieve requests."""
    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments/[^/]+$",
        lambda req, m: httpx.Response(
            200,
            json=_environment_response(
                environment_id=req.url.path.split("/")[-1], name="env-from-router"
            ).model_dump(mode="json"),
        ),
    )

    request = httpx.Request("GET", "https://api.anthropic.com/v1/environments/env_test456")
    response = router.dispatch(request)

    assert response.status_code == 200, (
        "MARouter should dispatch environment-retrieve to the handler"
    )
    assert response.json()["id"] == "env_test456", "response id should match the path segment"
