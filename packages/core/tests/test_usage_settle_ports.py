"""The settle path reads MA through the scoped session-tools port, byte for byte.

Every SDK uses an explicit MockTransport. The control in each test is the
SDK's own `sessions.events.list` / `sessions.retrieve` call with the same
arguments: the port must send the same requests and surface the same pages
and errors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from anthropic import APIStatusError, BadRequestError, NotFoundError
from daimon.core import usage_sweep
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.stores import session_settle_state
from mux.drivers.anthropic.resources.session_tools import EventPage, EventQuery, SessionTools
from mux.errors import ScopeViolation
from mux.testing.isolated_sdk import build_async_anthropic
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

AT = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
SESSION = "sess-native-page"
SCOPE = resource_scope(tenant_id="test-tenant", authorization_id="test-settle")
EVENT = {
    "id": "span-wire-1",
    "type": "span.model_request_end",
    "processed_at": AT.isoformat(),
    "is_error": False,
    "model_request_start_id": "span-start",
    "model_usage": {
        "input_tokens": 2,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
}
SETTLE_QUERY = {"order": "desc", "limit": 100, "types": ["span.model_request_end"]}


def _error(status: int) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error" if status == 400 else "not_found_error",
                "message": "cursor rejected",
            },
        },
    )


def _recorder(requests: list[tuple[object, ...]], *, empty=False, cursor="next", errors=()):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(
            (request.method, request.url.raw_path, tuple(request.headers.raw), request.content)
        )
        index = len(requests) - 1
        if index < len(errors) and errors[index] is not None:
            return _error(errors[index])
        token = request.url.params.get("page")
        return httpx.Response(
            200,
            json={
                "data": [] if empty else [EVENT],
                "next_page": None if token == "next" else cursor,
            },
        )

    return handle


def _dump(page) -> tuple[bool, list[dict[str, object]]]:
    return page.has_next_page(), [
        item.model_dump(mode="json", exclude_unset=True) for item in page.data
    ]


async def wire(*, ported, fields, empty=False, cursor="next", continue_page=False, errors=()):
    requests: list[tuple[object, ...]] = []
    client = build_async_anthropic(
        transport=httpx.MockTransport(
            _recorder(requests, empty=empty, cursor=cursor, errors=errors)
        )
    )
    pages = []
    failure = None
    try:
        if ported:
            page = await usage_sweep._list_events(
                client, SESSION, scope=SCOPE, query=EventQuery(**fields)
            )
        else:
            page = await client.beta.sessions.events.list(SESSION, **fields)
        pages.append(_dump(page))
        if continue_page and page.has_next_page():
            page = await page.get_next_page()
            pages.append(_dump(page))
    except (BadRequestError, NotFoundError) as error:
        failure = (type(error).__name__, error.status_code)
    finally:
        await client.close()
    return requests, pages, failure


@pytest.mark.parametrize(
    "fields",
    [
        SETTLE_QUERY,
        {**SETTLE_QUERY, "created_at_gte": AT},
        {**SETTLE_QUERY, "created_at_lt": AT + timedelta(hours=1)},
        {**SETTLE_QUERY, "created_at_gte": AT.isoformat(), "page": "saved"},
        {**SETTLE_QUERY, "created_at_gte": None, "created_at_lt": None, "page": None},
    ],
)
async def test_native_query_request_bytes_are_sdk_equal(fields):
    actual = await wire(ported=True, fields=fields)
    assert actual == await wire(ported=False, fields=fields)
    assert any(name.lower() == b"anthropic-beta" for name, _ in actual[0][0][2])


@pytest.mark.parametrize("fields", [SETTLE_QUERY, {**SETTLE_QUERY, "created_at_gte": AT}])
async def test_native_continuation_is_byte_equal_and_keeps_original_filters(fields):
    actual = await wire(ported=True, fields=fields, continue_page=True)
    assert actual == await wire(ported=False, fields=fields, continue_page=True)
    assert len(actual[0]) == 2


@pytest.mark.parametrize("empty,cursor", [(True, "next"), (False, ""), (False, None)])
async def test_empty_page_or_cursor_stops_like_sdk(empty, cursor):
    actual = await wire(
        ported=True, fields=SETTLE_QUERY, empty=empty, cursor=cursor, continue_page=True
    )
    assert actual == await wire(
        ported=False, fields=SETTLE_QUERY, empty=empty, cursor=cursor, continue_page=True
    )
    assert len(actual[0]) == 1


@pytest.mark.parametrize("status", [400, 404])
async def test_midpass_failure_propagates_with_the_sdk_error(status):
    actual = await wire(ported=True, fields=SETTLE_QUERY, continue_page=True, errors=(None, status))
    assert actual == await wire(
        ported=False, fields=SETTLE_QUERY, continue_page=True, errors=(None, status)
    )
    assert len(actual[0]) == 2  # no reset: the failed continuation is not re-opened
    assert actual[2] == ("BadRequestError" if status == 400 else "NotFoundError", status)


async def _cutoff(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO usage_reconcile_progress (managed_session_id, covered_through) "
                "VALUES (:id, :at)"
            ),
            {"id": session_settle_state.BACKLOG_CUTOFF_ID, "at": AT},
        )


@pytest.mark.parametrize(
    "cutoff,backlog,filters",
    [
        (False, False, {}),
        (True, False, {"created_at_gte": AT}),
        (True, True, {"created_at_lt": AT}),
    ],
)
async def test_a_settle_reads_like_the_sdk_under_its_tenant_scope(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch, cutoff, backlog, filters
):
    if cutoff:
        await _cutoff(db_session_factory)
    captured = []
    original = usage_sweep.managed_agents

    def backend(client, *, scope, resources):
        captured.append((scope, resources))
        return original(client, scope=scope, resources=resources)

    monkeypatch.setattr(usage_sweep, "managed_agents", backend)
    expected: list[tuple[object, ...]] = []
    client = build_async_anthropic(transport=httpx.MockTransport(_recorder(expected)))
    try:
        listed = [
            e.id async for e in client.beta.sessions.events.list(SESSION, **SETTLE_QUERY, **filters)
        ]
    finally:
        await client.close()
    actual: list[tuple[object, ...]] = []
    client = build_async_anthropic(transport=httpx.MockTransport(_recorder(actual)))
    try:
        result = await usage_sweep.settle_session(
            client, db_session_factory, SESSION, record=None, scope=SCOPE, backlog=backlog
        )
    finally:
        await client.close()
    assert actual == expected
    assert [e.id for e in result.unbilled] == listed and result.complete
    assert captured == [(SCOPE, frozenset({("session", SESSION)}))]


async def test_a_settle_without_a_scope_reads_under_the_platform_settle_scope(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch
):
    captured = []
    original = usage_sweep.managed_agents

    def backend(client, *, scope, resources):
        captured.append(scope)
        return original(client, scope=scope, resources=resources)

    monkeypatch.setattr(usage_sweep, "managed_agents", backend)
    client = build_async_anthropic(transport=httpx.MockTransport(_recorder([])))
    try:
        await usage_sweep.settle_session(client, db_session_factory, SESSION, record=None)
    finally:
        await client.close()
    assert len(captured) == 1 and captured[0].is_platform
    assert captured[0].authorization_id == "usage_sweep:settle_request"


async def test_a_settle_preserves_the_sdk_error_type_and_response(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    body = {"type": "error", "error": {"type": "api_error", "message": "fixture"}}
    client = build_async_anthropic(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503, json=body))
    )
    try:
        with pytest.raises(APIStatusError) as error:
            await usage_sweep.settle_session(
                client, db_session_factory, SESSION, record=None, scope=SCOPE
            )
    finally:
        await client.close()
    assert error.value.status_code == 503
    assert error.value.response.json() == body


async def test_the_drain_retrieves_under_the_platform_scope_and_keeps_not_found(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch
):
    async with db_session_factory() as s, s.begin():
        await session_settle_state.request_settle(s, managed_session_id=SESSION)
    scopes = []
    original = usage_sweep.retrieve_session_record

    async def retrieve(client, session_id, *, scope):
        scopes.append(scope)
        return await original(client, session_id, scope=scope)

    monkeypatch.setattr(usage_sweep, "retrieve_session_record", retrieve)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _error(404)

    client = build_async_anthropic(transport=httpx.MockTransport(handle))
    try:
        assert (
            await usage_sweep.sweep_headless_usage(client, db_session_factory, markup=Decimal("1"))
            == 0
        )
    finally:
        await client.close()
    # A NotFoundError (the SDK type, through the port) clears the request.
    assert [r.url.path for r in requests] == [f"/v1/sessions/{SESSION}"]
    assert len(scopes) == 1 and scopes[0].is_platform
    async with db_session_factory() as s:
        assert await session_settle_state.settle_request(s, managed_session_id=SESSION) is None


def _port(client):
    backend = managed_agents(client, scope=SCOPE, resources=frozenset({("session", SESSION)}))
    port = backend.extension(SessionTools, namespace="anthropic.session_tools", version=1)
    return port, resource_ref(backend, "session", SESSION, scope=SCOPE)


async def test_native_callback_is_private_and_json_cannot_recreate_authority():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [EVENT], "next_page": "next"})

    client = build_async_anthropic(transport=httpx.MockTransport(handle))
    try:
        port, session = _port(client)
        page = await port.list_events(SCOPE, session, EventQuery())
        encoded = page.model_dump_json()
        assert "_continue" not in encoded and "test-isolated-sdk" not in encoded
        assert "test-isolated-sdk" not in repr(page)
        clone = EventPage.model_validate_json(encoded)
        with pytest.raises(RuntimeError, match="authorized native"):
            await clone.continue_page()
        assert len(requests) == 1
    finally:
        await client.close()


async def test_native_continuation_rechecks_authorization(monkeypatch):
    from mux.drivers.anthropic.resources._authorization import ResourceAuthorization

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [EVENT], "next_page": "next"})

    client = build_async_anthropic(transport=httpx.MockTransport(handle))
    try:
        port, session = _port(client)
        page = await port.list_events(SCOPE, session, EventQuery())
        foreign = SCOPE.model_copy(update={"tenant_id": "foreign"})
        monkeypatch.setattr(
            port,
            "_authorization",
            ResourceAuthorization(foreign, frozenset({("session", SESSION)})),
        )
        with pytest.raises(ScopeViolation):
            await page.continue_page()
        assert len(requests) == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "field,value", [("tenant_id", "foreign"), ("provider", "gemini"), ("id", "other-session")]
)
async def test_foreign_reference_cannot_read_native_page(field, value):
    requests = []
    client = build_async_anthropic(transport=httpx.MockTransport(lambda r: requests.append(r)))
    try:
        port, session = _port(client)
        with pytest.raises(ScopeViolation):
            await port.list_events(SCOPE, session.model_copy(update={field: value}), EventQuery())
        assert not requests
    finally:
        await client.close()


def test_native_query_does_not_serialize_datetimes_before_sdk():
    query = EventQuery(created_at_gte=AT, created_at_lt=AT + timedelta(hours=1))
    fields = query.model_dump(exclude_unset=True)
    assert fields["created_at_gte"] is AT and isinstance(fields["created_at_lt"], datetime)
    assert set(fields) == {"created_at_gte", "created_at_lt"}
    assert EventQuery().model_dump(exclude_unset=True) == {}
    assert EventQuery(created_at_gte=None).model_dump(exclude_unset=True) == {
        "created_at_gte": None
    }
