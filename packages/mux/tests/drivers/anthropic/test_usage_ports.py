"""Model-span usage ports retain native requests, paging and scope checks."""

from collections import deque
from datetime import UTC, datetime

import httpx
import pytest
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.usage import AnthropicUsage, UsageWalk
from mux.errors import ExtensionVersionError, ProviderError, ScopeViolation

TENANT = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="grant"
)
SESSION = ResourceRef(
    id="sesn",
    kind="session",
    provider="anthropic",
    account_scope_id="workspace",
    tenant_id="tenant",
    account_id="account",
)


def span(event_id):
    return dict(
        id=event_id,
        type="span.model_request_end",
        processed_at="2026-10-09T00:00:00Z",
        is_error=False,
        model_request_start_id="start",
        model_usage=dict(
            input_tokens=10,
            output_tokens=40,
            cache_creation_input_tokens=30,
            cache_read_input_tokens=20,
            speed=None,
        ),
    )


def transport(pages):
    return ScriptedTransport(
        deque(
            ScriptedReply("GET", "/v1/sessions/sesn/events", httpx.Response(200, json=p))
            for p in pages
        )
    )


def driver(client):
    return AnthropicUsage(
        client, "workspace", ResourceAuthorization(TENANT, frozenset({("session", "sesn")}))
    )


async def test_factory_registers_usage_without_requests_and_keeps_injection():
    sdk = transport([])
    async with sdk.client() as client:
        backend = AnthropicManagedAgents(
            client,
            account_scope_id="workspace",
            authorization=ResourceAuthorization(TENANT, frozenset({("session", "sesn")})),
        )
        walk = backend.extension(UsageWalk, namespace="anthropic.usage_walk", version=1)
        assert walk is backend.usage
        with pytest.raises(ExtensionVersionError):
            backend.extension(UsageWalk, namespace="anthropic.usage_walk", version=2)
        injected = driver(client)
        assert AnthropicManagedAgents(client, usage=injected).usage is injected
        assert not client.is_closed()
    assert sdk.requests == []


async def test_registered_usage_walk_retains_native_request_and_measurement():
    pages = [dict(data=[span("one")], next_page=None)]
    baseline, sdk = transport(pages), transport(pages)
    async with baseline.client() as client:
        expected = [
            event
            async for event in client.beta.sessions.events.list(
                "sesn", order="asc", types=["span.model_request_end"]
            )
        ]
    async with sdk.client() as client:
        backend = AnthropicManagedAgents(
            client,
            account_scope_id="workspace",
            authorization=ResourceAuthorization(TENANT, frozenset({("session", "sesn")})),
        )
        walk = backend.extension(UsageWalk, namespace="anthropic.usage_walk", version=1)
        result = [o async for o in walk.model_requests(TENANT, SESSION, model_id="claude-model")]
    assert len(result) == 1 and result[0].id == "one" and result[0].input_tokens == 60
    assert result[0].model.id == "claude-model"
    assert [o.id for o in result] == [event.id for event in expected]
    assert sdk.requests == baseline.requests
    baseline.assert_consumed()
    sdk.assert_consumed()


async def test_walk_requests_match_the_original_sdk_paginator():
    pages = [dict(data=[span("one")], next_page="second"), dict(data=[span("two")], next_page=None)]
    old, new = transport(pages), transport(pages)
    async with old.client() as before, new.client() as after:
        expected = [
            e
            async for e in before.beta.sessions.events.list(
                "sesn", order="asc", types=["span.model_request_end"]
            )
        ]
        actual = [
            o async for o in driver(after).model_requests(TENANT, SESSION, model_id="claude-model")
        ]
    assert [o.id for o in actual] == [e.id for e in expected]
    assert actual[0].input_tokens == 60
    assert actual[0].native_meter == expected[0].model_usage.model_dump(mode="json")
    assert actual[0].model.id == "claude-model"
    assert actual[0].observed_at == datetime(2026, 10, 9, tzinfo=UTC)
    assert new.requests == old.requests
    old.assert_consumed()
    new.assert_consumed()


async def test_early_close_fetches_no_extra_page():
    sdk = transport(
        [dict(data=[span("one")], next_page="second"), dict(data=[span("two")], next_page=None)]
    )
    async with sdk.client() as client:
        walk = driver(client).model_requests(TENANT, SESSION)
        assert (await anext(walk)).id == "one"
        await walk.aclose()
    assert len(sdk.requests) == 1 and len(sdk.replies) == 1


async def test_recorded_ids_are_skipped_before_normalization_across_pages():
    recorded = span("recorded")
    recorded["model_usage"] = None
    pages = [
        dict(data=[recorded], next_page="second"),
        dict(data=[span("new")], next_page=None),
    ]
    old, new = transport(pages), transport(pages)
    async with old.client() as before, new.client() as after:
        expected = [
            event.id
            async for event in before.beta.sessions.events.list(
                "sesn", order="asc", types=["span.model_request_end"]
            )
            if event.id != "recorded"
        ]
        actual = [
            observation.id
            async for observation in driver(after).model_requests(
                TENANT, SESSION, exclude_ids=frozenset({"recorded"})
            )
        ]
    assert actual == expected == ["new"]
    assert new.requests == old.requests
    old.assert_consumed()
    new.assert_consumed()


async def test_foreign_scope_fails_before_any_provider_request():
    sdk = transport([])
    async with sdk.client() as client:
        foreign = SESSION.model_copy(update={"tenant_id": "foreign"})
        with pytest.raises(ScopeViolation):
            await anext(driver(client).model_requests(TENANT, foreign))
    assert sdk.requests == []


async def test_page_keeps_server_cursor_and_exact_sdk_kwargs():
    pages = [dict(data=[span("one")], next_page="second")]
    old, new = transport(pages), transport(pages)
    async with old.client() as before, new.client() as after:
        await before.beta.sessions.events.list(
            "sesn", page="first", limit=10, order="desc", types=["span.model_request_end"]
        )
        page = await driver(after).list(
            TENANT, SESSION, page=PageRequest(cursor="first", limit=10, order="desc")
        )
    assert page.has_more and page.next_cursor == "second"
    assert [o.id for o in page.data] == ["one"]
    assert new.requests == old.requests
    old.assert_consumed()
    new.assert_consumed()


async def test_next_page_error_is_normalized_with_original_cause():
    sdk = transport([dict(data=[span("one")], next_page="second")])
    sdk.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/sesn/events",
            httpx.Response(503, json={"error": {"type": "api_error", "message": "fixture"}}),
        )
    )
    async with sdk.client() as client:
        walk = driver(client).model_requests(TENANT, SESSION)
        await anext(walk)
        with pytest.raises(ProviderError) as error:
            await anext(walk)
    assert error.value.retryable and error.value.__cause__.status_code == 503
    sdk.assert_consumed()
