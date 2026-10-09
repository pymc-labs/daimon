"""The host span reader keeps SDK requests and uses tenant authorization."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from anthropic import APIStatusError
from daimon.core import usage_sweep
from daimon.testing.factories import make_platform_principal
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

TENANT = uuid.UUID(int=8)


def transport():
    sdk = ScriptedTransport()
    for event_id, next_page in (("one", "second"), ("two", None)):
        sdk.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/sesn/events",
                httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": event_id,
                                "type": "span.model_request_end",
                                "model_request_start_id": "start",
                                "is_error": False,
                                "processed_at": "2026-10-09T00:00:00Z",
                                "model_usage": {
                                    "input_tokens": 10,
                                    "output_tokens": 40,
                                    "cache_creation_input_tokens": 30,
                                    "cache_read_input_tokens": 20,
                                    "speed": None,
                                },
                            }
                        ],
                        "next_page": next_page,
                    },
                ),
            )
        )
    return sdk


async def test_span_reader_retains_requests_with_real_tenant_scope(monkeypatch):
    before, after = transport(), transport()
    captured = []
    original = usage_sweep.managed_agents

    def backend(client, *, scope, resources):
        captured.append((scope, resources))
        return original(client, scope=scope, resources=resources)

    monkeypatch.setattr(usage_sweep, "managed_agents", backend)
    session = ma_session(id="sesn", metadata={"daimon_tenant": str(TENANT)})
    async with before.client() as client:
        expected = [
            event
            async for event in client.beta.sessions.events.list(
                "sesn", order="asc", types=["span.model_request_end"]
            )
        ]
    async with after.client() as client:
        actual = [
            observation
            async for observation in usage_sweep._observations(client, session, tenant_id=TENANT)
        ]
    assert after.requests == before.requests
    assert [o.id for o in actual] == [e.id for e in expected]
    assert actual[0].model.id == session.agent.model.id
    assert actual[0].observed_at == datetime(2026, 10, 9, tzinfo=UTC)
    assert actual[0].session.tenant_id == str(TENANT)
    assert len(captured) == 1
    scope, resources = captured[0]
    assert scope.tenant_id == str(TENANT)
    assert not scope.is_platform and not scope.is_legacy_host_authorized
    assert resources == frozenset({("session", "sesn")})
    before.assert_consumed()
    after.assert_consumed()


async def test_session_inventory_preserves_pagination_native_status_and_host_filtering(
    db_session, db_session_factory, monkeypatch
):
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="inventory-fidelity"
    )
    rows = []
    for identity, metadata in (
        ("untagged", {}),
        ("invalid", {"daimon_tenant": "invalid-uuid"}),
        ("foreign", {"daimon_tenant": str(uuid.UUID(int=999))}),
        ("eligible", {"daimon_tenant": str(principal.tenant_id)}),
    ):
        row = ma_session(id=identity, metadata=metadata).model_dump(mode="json")
        row["status"] = "future-native-status"
        row["future_field"] = {"retained": True}
        rows.append(row)
    before, after = ScriptedTransport(), ScriptedTransport()
    for sdk in (before, after):
        for data, cursor in ((rows[:3], "next"), (rows[3:], None)):
            sdk.queue(
                ScriptedReply(
                    "GET",
                    "/v1/sessions",
                    httpx.Response(200, json={"data": data, "next_page": cursor}),
                )
            )
        sdk.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/eligible/events",
                httpx.Response(200, json={"data": [], "next_page": None}),
            )
        )
    async with before.client() as client:
        expected = [item async for item in client.beta.sessions.list()]
        _ = [
            event
            async for event in client.beta.sessions.events.list(
                "eligible", order="asc", types=["span.model_request_end"]
            )
        ]
    captured = []
    scopes = []
    original_reader = usage_sweep._observations
    original_backend = usage_sweep.managed_agents

    async def observations(client, session, **kwargs):
        captured.append(session.model_dump(mode="json", exclude_unset=True))
        async for observation in original_reader(client, session, **kwargs):
            yield observation

    def backend(client, *, scope, **kwargs):
        scopes.append(scope)
        return original_backend(client, scope=scope, **kwargs)

    monkeypatch.setattr(usage_sweep, "_observations", observations)
    monkeypatch.setattr(usage_sweep, "managed_agents", backend)
    async with after.client() as client:
        assert (
            await usage_sweep.sweep_headless_usage(client, db_session_factory, markup=Decimal("1"))
            == 0
        )
    assert captured == [expected[-1].model_dump(mode="json", exclude_unset=True)]
    assert captured[0]["status"] == "future-native-status"
    assert scopes[0].is_platform
    assert scopes[0].authorization_id == "usage_sweep:session_inventory"
    assert scopes[1].tenant_id == str(principal.tenant_id)
    assert not scopes[1].is_platform
    assert after.requests == before.requests
    before.assert_consumed()
    after.assert_consumed()


async def test_session_inventory_preserves_sdk_error_and_failed_watermark(db_session_factory):
    sdk = ScriptedTransport()
    sdk.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions",
            httpx.Response(503, json={"error": {"type": "api_error", "message": "fixture"}}),
        )
    )
    watermark = usage_sweep.UsageSweepWatermark()
    async with sdk.client() as client:
        with pytest.raises(APIStatusError) as error:
            await usage_sweep.sweep_headless_usage(
                client, db_session_factory, markup=Decimal("1"), watermark=watermark
            )
    assert error.value.status_code == 503
    assert error.value.response.json() == {"error": {"type": "api_error", "message": "fixture"}}
    assert watermark.last_successful_start is None
    assert watermark.last_full_start is None
    sdk.assert_consumed()


async def test_span_reader_preserves_sdk_error_type_and_response():
    sdk = ScriptedTransport()
    sdk.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/sesn/events",
            httpx.Response(503, json={"error": {"type": "api_error", "message": "fixture"}}),
        )
    )
    async with sdk.client() as client:
        with pytest.raises(APIStatusError) as error:
            await anext(usage_sweep._observations(client, ma_session(id="sesn"), tenant_id=TENANT))
    assert error.value.status_code == 503
    assert error.value.response.json() == {"error": {"type": "api_error", "message": "fixture"}}
    sdk.assert_consumed()


async def test_span_reader_passes_recorded_ids_to_walk_across_pages():
    before, after = transport(), transport()
    async with before.client() as client:
        expected = [
            event.id
            async for event in client.beta.sessions.events.list(
                "sesn", order="asc", types=["span.model_request_end"]
            )
            if event.id != "one"
        ]
    async with after.client() as client:
        actual = [
            observation.id
            async for observation in usage_sweep._observations(
                client,
                ma_session(id="sesn"),
                tenant_id=TENANT,
                exclude_ids=frozenset({"one"}),
            )
        ]
    assert actual == expected == ["two"]
    assert after.requests == before.requests
    before.assert_consumed()
    after.assert_consumed()
