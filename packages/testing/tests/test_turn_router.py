"""Tests for daimon.testing.turn_router, driven through a real AsyncAnthropic."""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Iterator
from typing import Any

import anthropic
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event import (
    BetaManagedAgentsUserMessageEvent,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.testing.ma import MARouter, build_fake_anthropic, session_response
from daimon.testing.turn_router import (
    AGENT_ID,
    AGENT_TEXT,
    ENV_ID,
    MODEL_ID,
    build_turn_router,
    turn_events,
)


@contextlib.contextmanager
def _transport_assertion(match: str) -> Iterator[None]:
    """The SDK wraps an exception raised inside the transport in
    `APIConnectionError`; the fake's AssertionError is its `__cause__`."""
    with pytest.raises(anthropic.APIConnectionError) as excinfo:
        yield
    cause = excinfo.value.__cause__
    assert isinstance(cause, AssertionError), f"expected the fake's AssertionError, got {cause!r}"
    assert re.search(match, str(cause)), f"{str(cause)!r} does not match {match!r}"


async def _collect_stream_event_types(client: AsyncAnthropic, session_id: str) -> list[str]:
    stream = await client.beta.sessions.events.stream(session_id)
    return [event.type async for event in stream]


async def test_build_turn_router_resolves_agent_and_environment_for_the_tenant() -> None:
    tenant_id = str(uuid.uuid4())
    client = build_fake_anthropic(build_turn_router(tenant_id).dispatch)

    agents = [agent async for agent in client.beta.agents.list()]
    assert [agent.id for agent in agents] == [AGENT_ID], "the list route must serve the agent"
    assert agents[0].metadata == {
        MA_METADATA_KEY_TENANT: tenant_id,
        MA_METADATA_KEY_NAME: "test-agent",
    }, "the agent must carry the tenant stamps the resolver matches on"
    assert agents[0].model.id == MODEL_ID, "the agent must run the router's model"

    retrieved = await client.beta.agents.retrieve("ag_any_other_id")
    assert retrieved.id == AGENT_ID, "the retrieve route answers any id with the one agent"

    environments = [env async for env in client.beta.environments.list()]
    assert [env.id for env in environments] == [ENV_ID], "the list route must serve the env"
    assert (await client.beta.environments.retrieve(ENV_ID)).metadata[
        MA_METADATA_KEY_TENANT
    ] == tenant_id, "the environment must be stamped for the tenant"


async def test_build_turn_router_drives_send_and_stream_end_to_end() -> None:
    client = build_fake_anthropic(build_turn_router(str(uuid.uuid4())).dispatch)

    sent = await client.beta.sessions.events.send(
        "sess_x", events=[{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]
    )
    assert sent.data is None, "send-events must answer with the empty data body by default"

    stream = await client.beta.sessions.events.stream("sess_x")
    events = [event async for event in stream]
    assert [event.type for event in events] == [
        "agent.message",
        "span.model_request_end",
        "session.status_idle",
    ], "the stream must emit message, usage, then the terminal idle event"
    message = events[0]
    assert message.type == "agent.message", "first event must be the agent message"
    assert message.content[0].type == "text" and message.content[0].text == AGENT_TEXT, (
        "the agent message must carry the configured text"
    )
    usage = events[1]
    assert usage.type == "span.model_request_end", "second event must be the usage event"
    assert usage.id == "evt_parity_usage", "the usage event must carry the default id"
    assert (usage.model_usage.input_tokens, usage.model_usage.output_tokens) == (100, 50), (
        "the usage event must carry the default token counts"
    )


async def test_build_turn_router_without_usage_event_omits_it() -> None:
    client = build_fake_anthropic(
        build_turn_router(str(uuid.uuid4()), usage_event_id=None, agent_text="custom").dispatch
    )
    stream = await client.beta.sessions.events.stream("sess_x")
    events = [event async for event in stream]
    assert [event.type for event in events] == ["agent.message", "session.status_idle"], (
        "usage_event_id=None must drop the span.model_request_end event"
    )
    first = events[0]
    assert first.type == "agent.message" and first.content[0].type == "text", "message first"
    assert first.content[0].text == "custom", "agent_text= must be the message text"


async def test_build_turn_router_fresh_event_ids_change_per_stream_open() -> None:
    client = build_fake_anthropic(
        build_turn_router(str(uuid.uuid4()), fresh_event_ids=True).dispatch
    )

    async def usage_id() -> str:
        stream = await client.beta.sessions.events.stream("sess_x")
        ids = [event.id async for event in stream if event.type == "span.model_request_end"]
        assert len(ids) == 1, "each turn must emit exactly one usage event"
        return ids[0]

    first, second = await usage_id(), await usage_id()
    assert first != second, "fresh_event_ids must mint a new usage event id per stream open"
    assert first.startswith("evt_parity_usage"), "fresh ids must keep the configured prefix"


async def test_build_turn_router_records_sent_bodies_and_stream_hits() -> None:
    sent_event_bodies: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    client = build_fake_anthropic(
        build_turn_router(
            str(uuid.uuid4()), sent_event_bodies=sent_event_bodies, stream_hits=stream_hits
        ).dispatch
    )
    await client.beta.sessions.events.send(
        "sess_one", events=[{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]
    )
    await _collect_stream_event_types(client, "sess_one")
    await _collect_stream_event_types(client, "sess_two")

    assert len(sent_event_bodies) == 1, "every send must be recorded"
    assert sent_event_bodies[0]["events"][0]["content"][0]["text"] == "hi", (
        "the recorded body must be the JSON the SDK sent"
    )
    assert stream_hits == ["sess_one", "sess_two"], (
        "every stream open must record the session id it ran on"
    )


async def test_build_turn_router_send_events_data_is_echoed() -> None:
    boundary = BetaManagedAgentsUserMessageEvent(
        id="sevt_boundary",
        content=[BetaManagedAgentsTextBlock(type="text", text="hello")],
        type="user.message",
    ).model_dump(mode="json")
    client = build_fake_anthropic(
        build_turn_router(str(uuid.uuid4()), send_events_data=[boundary]).dispatch
    )
    sent = await client.beta.sessions.events.send(
        "sess_x", events=[{"type": "user.message", "content": [{"type": "text", "text": "hello"}]}]
    )
    assert sent.data is not None and [event.id for event in sent.data] == ["sevt_boundary"], (
        "send_events_data= must be the `data` the send route returns"
    )


async def test_build_turn_router_session_id_scopes_the_event_routes() -> None:
    client = build_fake_anthropic(
        build_turn_router(str(uuid.uuid4()), session_id="sess_only").dispatch
    )

    assert await _collect_stream_event_types(client, "sess_only"), "the scoped session streams"
    with _transport_assertion("no route for GET /v1/sessions/sess_other"):
        await _collect_stream_event_types(client, "sess_other")


async def test_build_turn_router_composes_with_an_existing_router() -> None:
    router = MARouter()
    router.add("GET", r"/v1/sessions/[^/]+", lambda _r, m: session_response(session_id="sess_x"))
    returned = build_turn_router(str(uuid.uuid4()), router=router)
    assert returned is router, "router= must add routes to the given router and return it"

    client = build_fake_anthropic(router.dispatch)
    assert (await client.beta.sessions.retrieve("sess_x")).id == "sess_x", (
        "the caller's own session route must survive alongside the turn routes"
    )
    assert await _collect_stream_event_types(client, "sess_x") == [
        "agent.message",
        "span.model_request_end",
        "session.status_idle",
    ], "the turn routes must be registered on the caller's router"


def test_turn_events_suffix_and_usage_knobs() -> None:
    with_suffix = turn_events(
        event_id_suffix="_7", usage_event_id="evt_u", input_tokens=1, output_tokens=2
    )
    assert [event["id"] for event in with_suffix] == [
        "evt_parity_msg_7",
        "evt_u_7",
        "evt_parity_idle_7",
    ], "the suffix must land on every event id"
    assert with_suffix[1]["model_usage"]["input_tokens"] == 1, "token counts must be configurable"
    assert [event["type"] for event in turn_events(usage_event_id=None)] == [
        "agent.message",
        "session.status_idle",
    ], "usage_event_id=None must drop the usage event"
