"""Real SDK paging keeps normalization state through a full event history."""

from datetime import UTC, datetime

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.events import Event, RequiresActionPayload
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.turn import EventHistoryWalk
from mux.errors import ProviderError, ScopeViolation
from pydantic import JsonValue

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="grant"
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="anthropic",
    account_scope_id="workspace",
    tenant_id="tenant",
    account_id="account",
)
AUTH = ResourceAuthorization(SCOPE, frozenset({("session", "session")}))


def native(kind: str, id: str, **fields: JsonValue) -> dict[str, JsonValue]:
    return {
        "type": kind,
        "id": id,
        "processed_at": datetime(2026, 10, 9, tzinfo=UTC).isoformat(),
        **fields,
    }


def history(client: AsyncAnthropic) -> EventHistoryWalk:
    backend = AnthropicManagedAgents(client, account_scope_id="workspace", authorization=AUTH)
    return backend.extension(EventHistoryWalk, namespace="anthropic.event_history", version=1)


async def test_normalizer_spans_sdk_pages_and_preserves_exact_paginator_requests():
    first = [
        native("session.status_running", "root"),
        native(
            "agent.mcp_tool_use",
            "call",
            name="run",
            input={"command": "pwd"},
            mcp_server_name="tools",
            evaluated_permission="ask",
        ),
    ]
    second = [
        native(
            "session.status_idle",
            "paused",
            stop_reason={"type": "requires_action", "event_ids": ["call"]},
        )
    ]
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/session/events",
            httpx.Response(200, json={"data": first, "next_page": "page2"}),
            query=(("beta", "true"),),
        ),
        ScriptedReply(
            "GET",
            "/v1/sessions/session/events",
            httpx.Response(200, json={"data": second, "next_page": None}),
            query=(("beta", "true"), ("page", "page2")),
        ),
    )
    async with transport.client() as client:
        events = [event async for event in history(client).walk(SCOPE, SESSION)]
    assert [event.sequence for event in events] == [0, 1, 2]
    assert [event.turn_id for event in events] == ["root"] * 3
    assert events[-1].type == "session.requires_action"
    payload = events[-1].typed_payload()
    assert isinstance(payload, RequiresActionPayload)
    action = payload.actions[0]
    assert action.id == "call" and action.kind == "tool_confirmation"
    assert action.payload["mcp_server_name"] == "tools"
    assert action.payload["evaluated_permission"] == "ask"
    assert all(isinstance(event, Event) and event.authority == "record" for event in events)
    transport.assert_consumed()
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    "records,cursor", [([], "next"), ([native("session.status_running", "root")], "")]
)
async def test_history_uses_sdk_terminal_page_rule_without_extra_get(
    records: list[dict[str, JsonValue]], cursor: str
) -> None:
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/session/events",
            httpx.Response(200, json={"data": records, "next_page": cursor}),
        )
    )
    async with transport.client() as client:
        events = [event async for event in history(client).walk(SCOPE, SESSION)]
    assert len(events) == len(records)
    transport.assert_consumed()
    assert len(transport.requests) == 1


async def test_later_page_sdk_error_is_owned_and_keeps_its_original_cause():
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/session/events",
            httpx.Response(
                200, json={"data": [native("session.status_running", "root")], "next_page": "page2"}
            ),
        ),
        ScriptedReply(
            "GET",
            "/v1/sessions/session/events",
            httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "busy"}}),
        ),
    )
    async with transport.client() as client:
        with pytest.raises(ProviderError) as caught:
            _ = [event async for event in history(client).walk(SCOPE, SESSION)]
    assert caught.value.category == "rate_limited"
    assert caught.value.__cause__ is not None
    transport.assert_consumed()


async def test_history_foreign_scope_refuses_before_paginator_io():
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(ScopeViolation):
            _ = [
                event
                async for event in history(client).walk(
                    SCOPE.model_copy(update={"tenant_id": "other"}), SESSION
                )
            ]
    assert transport.requests == []


async def test_malformed_history_record_raises_owned_translation_error():
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/session/events",
            httpx.Response(
                200,
                json={
                    "data": [native("agent.tool_result", "result", tool_use_id="call", content={})],
                    "next_page": None,
                },
            ),
        )
    )
    async with transport.client() as client:
        with pytest.raises(ProviderError) as caught:
            _ = [event async for event in history(client).walk(SCOPE, SESSION)]
    assert caught.value.native_code == "malformed_event" and not caught.value.retryable
    transport.assert_consumed()
