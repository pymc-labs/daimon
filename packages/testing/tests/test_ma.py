"""Smoke tests for daimon.testing.ma module.

Validates MA handler factories, combine_handlers, build_fake_anthropic,
build_stub_anthropic, make_fake_ma_handler, MARouter, NotHandled, and
shared constants.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Iterator

import anthropic
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
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session


@contextlib.contextmanager
def _transport_assertion(match: str) -> Iterator[None]:
    """The SDK wraps an exception raised inside the transport in
    `APIConnectionError`; the fake's AssertionError is its `__cause__`."""
    with pytest.raises(anthropic.APIConnectionError) as excinfo:
        yield
    cause = excinfo.value.__cause__
    assert isinstance(cause, AssertionError), f"expected the fake's AssertionError, got {cause!r}"
    assert re.search(match, str(cause)), f"{str(cause)!r} does not match {match!r}"


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


# ---------------------------------------------------------------------------
# Router conveniences and shared handlers
# ---------------------------------------------------------------------------


async def test_resolved_agent_env_router_serves_list_and_retrieve_for_both() -> None:
    from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
    from daimon.testing.ma import resolved_agent_env_router

    tenant_id = uuid.uuid4()
    client = build_fake_anthropic(resolved_agent_env_router(tenant_id=tenant_id).dispatch)

    agents = [agent async for agent in client.beta.agents.list()]
    assert [agent.id for agent in agents] == [ma_agent().id], "list must serve the default agent"
    assert agents[0].metadata[MA_METADATA_KEY_TENANT] == str(tenant_id), (
        "the served agent must be stamped for the tenant so the resolver's tag lookup finds it"
    )
    assert (await client.beta.agents.retrieve(ma_agent().id)).id == ma_agent().id, (
        "retrieve must serve the same agent by its exact id"
    )
    environments = [env async for env in client.beta.environments.list()]
    assert [env.id for env in environments] == [ma_environment().id], "list must serve the env"
    assert (await client.beta.environments.retrieve(ma_environment().id)).metadata[
        MA_METADATA_KEY_TENANT
    ] == str(tenant_id), "retrieve must serve the same tenant-stamped environment"


async def test_resolved_agent_env_router_uses_given_shapes_and_exact_ids() -> None:
    from daimon.testing.ma import resolved_agent_env_router

    agent = ma_agent(id="ag_given", name="given")
    environment = ma_environment(id="env_given")
    router = MARouter()
    returned = resolved_agent_env_router(agent, environment, router=router)
    assert returned is router, "router= must add the routes to the given router and return it"

    client = build_fake_anthropic(router.dispatch)
    assert (await client.beta.agents.retrieve("ag_given")).name == "given", "given agent served"
    assert (await client.beta.environments.retrieve("env_given")).id == "env_given", "given env"
    with _transport_assertion("no route for GET /v1/agents/ag_other"):
        await client.beta.agents.retrieve("ag_other")


async def test_marouter_add_session_serves_the_exact_session_id() -> None:
    router = MARouter()
    router.add_session(ma_session(id="sess_exact", agent_id="ag_frozen"))
    client = build_fake_anthropic(router.dispatch)

    session = await client.beta.sessions.retrieve("sess_exact")
    assert session.agent.id == "ag_frozen", "the served session must carry its frozen agent"
    with _transport_assertion("no route for GET /v1/sessions/sess_other"):
        await client.beta.sessions.retrieve("sess_other")


async def test_marouter_add_agent_list_serves_every_given_agent() -> None:
    router = MARouter()
    router.add_agent_list(ma_agent(id="ag_1"), ma_agent(id="ag_2"))
    router.add_environment_list()
    client = build_fake_anthropic(router.dispatch)

    assert [agent.id async for agent in client.beta.agents.list()] == ["ag_1", "ag_2"], (
        "add_agent_list must serve the agents in the given order"
    )
    assert [env.id async for env in client.beta.environments.list()] == [], (
        "an empty add_environment_list must serve an empty list"
    )


async def test_make_agent_env_echo_handler_returns_the_requested_ids() -> None:
    from daimon.testing.ma import make_agent_env_echo_handler

    client = build_fake_anthropic(make_agent_env_echo_handler(tenant_id="tenant-x"))

    agent = await client.beta.agents.retrieve("ag_asked")
    assert agent.id == "ag_asked", "the echo handler must answer with the id that was asked for"
    assert agent.metadata["daimon_tenant"] == "tenant-x", "tenant_id= must stamp the agent"
    assert (await client.beta.environments.retrieve("env_asked")).id == "env_asked", (
        "environments echo their id too"
    )
    assert (await client.beta.sessions.retrieve("sess_asked")).id == "sess_asked", (
        "with_session=True (the default) serves session retrieves"
    )
    with _transport_assertion("unhandled GET /v1/agents"):
        [agent async for agent in client.beta.agents.list()]


async def test_make_agent_env_echo_handler_without_session_rejects_session_reads() -> None:
    from daimon.testing.ma import make_agent_env_echo_handler

    client = build_fake_anthropic(make_agent_env_echo_handler(with_session=False))
    assert (await client.beta.agents.retrieve("ag_asked")).metadata == {}, (
        "without tenant_id= the echoed agent carries no metadata"
    )
    with _transport_assertion("unhandled GET /v1/sessions/sess_asked"):
        await client.beta.sessions.retrieve("sess_asked")


async def test_make_archive_agent_handler_archives_the_requested_agent_and_composes() -> None:
    from daimon.testing.ma import make_archive_agent_handler

    client = build_fake_anthropic(
        combine_handlers(make_archive_agent_handler(name="gone"), make_fake_ma_handler())
    )
    archived = await client.beta.agents.archive("ag_doomed")
    assert archived.id == "ag_doomed", "the archive response must name the archived agent"
    assert archived.archived_at is not None, "the archived agent must carry archived_at"
    assert archived.name == "gone" and archived.version == 2, "name= and the bumped version"

    created = await client.beta.agents.create(name="still-here", model="claude-sonnet-4-6")
    assert created.name == "still-here", (
        "non-archive requests must fall through to the next handler"
    )


async def test_build_no_retry_anthropic_does_not_retry_a_conflict() -> None:
    from daimon.testing.ma import build_no_retry_anthropic

    hits: list[str] = []

    def conflict(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        hits.append(request.url.path)
        return httpx.Response(
            409, json={"type": "error", "error": {"type": "conflict_error", "message": "stale"}}
        )

    router = MARouter()
    router.add("GET", r"/v1/agents/[^/]+", conflict)
    client = build_no_retry_anthropic(router)
    with pytest.raises(anthropic.APIStatusError):
        await client.beta.agents.retrieve("ag_x")
    assert hits == ["/v1/agents/ag_x"], "max_retries=0: the SDK must not retry the 409"

    client_from_handler = build_no_retry_anthropic(router.dispatch)
    with pytest.raises(anthropic.APIStatusError):
        await client_from_handler.beta.agents.retrieve("ag_y")
    assert hits[-1] == "/v1/agents/ag_y", "a plain handler must be accepted too"


def test_require_api_key_skips_without_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from daimon.testing.ma import require_api_key

    monkeypatch.delenv("DAIMON_TEST_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_api_key()
    monkeypatch.setenv("DAIMON_TEST_ANTHROPIC_API_KEY", "sk-live")
    assert require_api_key() == "sk-live", "the key must be returned when set"
