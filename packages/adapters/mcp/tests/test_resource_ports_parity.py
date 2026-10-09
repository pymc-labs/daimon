"""MCP resource ports preserve SDK bytes, pages, partial replies and tenant scopes."""

import datetime as dt
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.adapters.mcp import resource_ports
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _session_gate, agent_chat, sessions
from daimon.adapters.mcp.tools.agent_chat import (
    _archive_my_session_impl,  # pyright: ignore[reportPrivateUsage]
    _cancel_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _continue_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _get_session_impl,  # pyright: ignore[reportPrivateUsage]
    _get_turn_cost_impl,  # pyright: ignore[reportPrivateUsage]
    _list_events_impl,  # pyright: ignore[reportPrivateUsage]
    _start_turn_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.agent_chat import (
    _list_sessions_impl as _list_agent_sessions_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.sessions import (
    _list_session_events_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.sessions import (
    _list_sessions_impl as _list_tenant_sessions_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import bundle_handle, session_ports_compat
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import managed_agents, resource_ref
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.stores.domain import Role, ThreadSessionRow, TurnOriginRow
from daimon.testing.ma_models import ma_agent, ma_environment, ma_model_usage, ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from mux.drivers.anthropic.resources.session_tools import EventQuery, SessionSend, SessionTools
from mux.errors import ScopeViolation
from pydantic import BaseModel, PostgresDsn, SecretStr

TENANT = uuid.UUID(int=1)
ACCOUNT = uuid.UUID(int=2)
AGENT = ma_agent(id="agent", tenant_id=TENANT)
AUTH = AuthIdentity(
    tenant_id=TENANT,
    account_id=ACCOUNT,
    role=Role.USER,
    agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id="agent"),
)
SCOPE = resource_ports.mcp_scope(AUTH)
SESSION = ma_session(
    id="session",
    agent=AGENT,
    metadata={"daimon_tenant": str(TENANT), "daimon_account": str(ACCOUNT)},
)
BODY = SESSION.model_dump(mode="json")
MESSAGE = [{"type": "user.message", "content": [{"type": "text", "text": 'hi "世界"\n'}]}]
ECHO = {"data": [{"id": "accepted", "type": "user.message", "processed_at": None}]}


def script(replies: list[tuple[str, str, int, dict[str, Any]]]) -> ScriptedTransport:
    return ScriptedTransport(
        deque(
            ScriptedReply(method, path, httpx.Response(status, json=body))
            for method, path, status, body in replies
        )
    )


def same(old: ScriptedTransport, new: ScriptedTransport) -> None:
    old.assert_consumed()
    new.assert_consumed()
    assert [r.to_dict() for r in old.requests] == [r.to_dict() for r in new.requests]
    # Parsed JSON equality misses serialization/key-order regressions.
    assert [r.body for r in old.requests] == [r.body for r in new.requests]


def same_model(old: BaseModel, new: BaseModel) -> None:
    assert old.model_dump(mode="json") == new.model_dump(mode="json")
    assert old.model_dump(mode="json", exclude_unset=True) == new.model_dump(
        mode="json", exclude_unset=True
    )
    assert old.model_fields_set == new.model_fields_set


@pytest.mark.parametrize("initial", [None, "", "opaque-start"])
@pytest.mark.parametrize("stop", ["last", "next", "empty-cursor", "empty-next"])
async def test_walk_uses_the_original_async_paginator(
    initial: str | None, stop: Literal["last", "next", "empty-cursor", "empty-next"]
) -> None:
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        (
            "GET",
            "/v1/sessions",
            200,
            {
                "data": [] if stop == "empty-cursor" else [BODY, {"id": "partial"}],
                "next_page": "next"
                if stop in {"next", "empty-cursor"}
                else ""
                if stop == "empty-next"
                else None,
            },
        )
    ]
    if stop == "next":
        replies.append(("GET", "/v1/sessions", 200, {"data": [BODY], "next_page": None}))
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        kwargs: dict[str, Any] = {"agent_id": "agent"}
        if initial is not None:
            kwargs["page"] = initial
        expected = [item async for item in before.beta.sessions.list(**kwargs)]
        actual = [
            item
            async for item in resource_ports.walk_sessions(
                after, agent_id="agent", scope=SCOPE, page=initial
            )
        ]
        assert len(actual) == len(expected)
        for a, b in zip(expected, actual, strict=True):
            same_model(a, b)
    same(old, new)


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"page": "", "limit": 0, "order": "desc"},
        {
            "page": "opaque",
            "limit": 17,
            "order": "asc",
            "created_at_gte": "2026-10-09T12:00:00+00:00",
            "types": ["span.model_request_end", "session.thread_status_idle"],
        },
        {"types": []},
    ],
)
@pytest.mark.parametrize(
    "body",
    [
        {"data": [], "next_page": "empty-but-opaque"},
        {
            "data": [
                {"id": "unknown", "type": "session.thread_status_idle", "extra": {"keep": True}}
            ],
            "next_page": "",
        },
        {
            "data": [
                {"id": "e", "type": "agent.message", "content": [{"type": "text", "text": "hello"}]}
            ],
            "next_page": None,
        },
    ],
)
async def test_event_page_preserves_filters_unknown_rows_and_opaque_cursor(
    query: dict[str, Any], body: dict[str, Any]
) -> None:
    replies = [("GET", "/v1/sessions/session/events", 200, body)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.sessions.events.list("session", **query)
        actual = await resource_ports.list_session_events(
            after, "session", query=query, scope=SCOPE
        )
        assert actual.next_page == expected.next_page
        for a, b in zip(expected.data, actual.data, strict=True):
            same_model(a, b)
    same(old, new)


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("body", [ECHO, {"data": []}, {}, {"data": [{"id": "accepted"}]}])
async def test_send_preserves_echo_and_wire_bytes(interrupt: bool, body: dict[str, Any]) -> None:
    events: Any = [{"type": "user.interrupt"}] if interrupt else MESSAGE
    replies = [("POST", "/v1/sessions/session/events", 200, body)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.sessions.events.send("session", events=events)
        actual = await resource_ports.send_session_events(
            after, "session", events=events, scope=SCOPE
        )
        same_model(expected, actual)
    same(old, new)


@pytest.mark.parametrize("body", [{}, {"id": "file", "filename": "bundle.tar.gz"}])
async def test_file_existence_read_does_not_validate_unused_fields(body: dict[str, Any]) -> None:
    replies = [("GET", "/v1/files/file", 200, body)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.files.retrieve_metadata("file")
        actual = await resource_ports.retrieve_file_metadata(after, "file", scope=SCOPE)
        same_model(expected, actual)
    same(old, new)


@pytest.mark.parametrize("status", [404, 429, 500])
async def test_send_keeps_sdk_status_errors(status: int) -> None:
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        (
            "POST",
            "/v1/sessions/session/events",
            status,
            {"type": "error", "error": {"type": "api_error", "message": "rejected"}},
        )
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        with pytest.raises(APIStatusError) as expected:
            await before.beta.sessions.events.send("session", events=cast(Any, MESSAGE))
        with pytest.raises(type(expected.value)) as actual:
            await resource_ports.send_session_events(
                after, "session", events=cast(Any, MESSAGE), scope=SCOPE
            )
        assert actual.value.status_code == expected.value.status_code
        assert actual.value.body == expected.value.body
    same(old, new)


class SessionFactory:
    async def __aenter__(self) -> "SessionFactory":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def __call__(self) -> "SessionFactory":
        return self

    def begin(self) -> "SessionFactory":
        return self


@asynccontextmanager
async def fence(*args: object) -> AsyncIterator[None]:
    yield


def runtime(client: AsyncAnthropic) -> McpRuntime:
    return cast(
        Any,
        SimpleNamespace(
            client=client,
            session_factory=SessionFactory(),
            settings=Settings(
                database=DatabaseSettings(
                    url=PostgresDsn("postgresql+asyncpg://offline.invalid/n6")
                ),
                anthropic=AnthropicSettings(api_key=SecretStr("n6-offline-test-key")),
            ),
            fernet=None,
        ),
    )


@pytest.fixture
def host_guards(monkeypatch: pytest.MonkeyPatch) -> list[Scope]:
    scopes: list[Scope] = []
    for module in (resource_ports, session_ports_compat):
        original = module.managed_agents

        def make_backend(
            client: AsyncAnthropic,
            *,
            scope: Scope,
            resources: frozenset[tuple[str, str]],
            _original: Any = original,
        ) -> Any:
            assert scope.tenant_id == str(TENANT)
            assert scope.account_id == str(ACCOUNT)
            assert not scope.is_platform
            assert not scope.is_legacy_host_authorized
            scopes.append(scope)
            return _original(client, scope=scope, resources=resources)

        monkeypatch.setattr(module, "managed_agents", make_backend)
    monkeypatch.setattr(agent_chat, "require_session_outside_seals", AsyncMock())
    monkeypatch.setattr(sessions, "require_session_outside_seals", AsyncMock())
    monkeypatch.setattr(agent_chat, "session_mutation_fence", fence)
    monkeypatch.setattr(agent_chat, "close_headless_app_session", AsyncMock())
    monkeypatch.setattr(agent_chat, "touch_unmapped_app_session", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_chat, "_bound_seal", AsyncMock(return_value=frozenset()))
    return scopes


@pytest.mark.parametrize("operation", ["get", "cancel", "archive"])
async def test_agent_session_tools_keep_ownership_then_mutation_order(
    operation: Literal["get", "cancel", "archive"], host_guards: list[Scope]
) -> None:
    replies = [("GET", "/v1/sessions/session", 200, BODY)]
    if operation == "cancel":
        replies += [
            ("POST", "/v1/sessions/session/events", 200, ECHO),
            ("GET", "/v1/sessions/session", 200, BODY),
        ]
    elif operation == "archive":
        replies += [("POST", "/v1/sessions/session/archive", 200, {"id": "session"})]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.sessions.retrieve("session")
        if operation == "cancel":
            await before.beta.sessions.events.send("session", events=[{"type": "user.interrupt"}])
            expected = await before.beta.sessions.retrieve("session")
            actual = await _cancel_turn_impl(runtime(after), AUTH, "session")  # pyright: ignore[reportPrivateUsage]
            assert actual == {"handle": "session", "status": expected.status}
        elif operation == "archive":
            await before.beta.sessions.archive("session")
            await _archive_my_session_impl(runtime(after), AUTH, "session")  # pyright: ignore[reportPrivateUsage]
        else:
            actual = await _get_session_impl(runtime(after), AUTH, "session")  # pyright: ignore[reportPrivateUsage]
            assert actual == sessions.SessionInfo.from_ma(expected)
    assert host_guards
    same(old, new)


@pytest.mark.parametrize("owner", ["agent", "tenant"])
async def test_public_event_tools_keep_retrieve_then_filtered_page(
    owner: Literal["agent", "tenant"], host_guards: list[Scope], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "list_agents_by_tenant", AsyncMock(return_value=[AGENT]))
    query: dict[str, Any] = {"page": "opaque", "limit": 7, "order": "desc"}
    if owner == "agent":
        query.update(created_at_gte="2026-10-09T12:00:00Z", types=["session.thread_status_idle"])
    event_body = {
        "data": [{"id": "event", "type": "session.thread_status_idle", "extra": "kept"}],
        "next_page": "",
    }
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        ("GET", "/v1/sessions/session", 200, BODY),
        ("GET", "/v1/sessions/session/events", 200, event_body),
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        await before.beta.sessions.retrieve("session")
        expected = await before.beta.sessions.events.list("session", **query)
        if owner == "agent":
            actual = await _list_events_impl(
                runtime(after),
                AUTH,
                "session",
                "opaque",
                7,
                "desc",
                created_at_gte=query["created_at_gte"],
                types=query["types"],
            )  # pyright: ignore[reportPrivateUsage]
        else:
            actual = await _list_session_events_impl(
                runtime(after), AUTH, "session", "opaque", 7, "desc"
            )  # pyright: ignore[reportPrivateUsage]
        assert actual.items == [
            sessions.SessionEventOut.model_validate(e.model_dump(mode="json"))
            for e in expected.data
        ]
        assert actual.next_page == expected.next_page
    assert host_guards
    same(old, new)


@pytest.mark.parametrize("owner", ["agent", "tenant-filtered", "tenant-all"])
async def test_public_session_lists_keep_async_loop_and_account_filter(
    owner: str, host_guards: list[Scope], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_chat, "_resolve_ma_agent", AsyncMock(return_value=AGENT))
    monkeypatch.setattr(sessions, "find_agent_by_daimon_tag", AsyncMock(return_value=AGENT))
    monkeypatch.setattr(sessions, "list_agents_by_tenant", AsyncMock(return_value=[AGENT]))

    async def visible(
        _runtime: McpRuntime, _auth: AuthIdentity, rows: list[Any], **kwargs: object
    ) -> list[Any]:
        return rows

    monkeypatch.setattr(agent_chat, "sessions_outside_seals", visible)
    monkeypatch.setattr(sessions, "sessions_outside_seals", visible)
    other = ma_session(id="other-account", agent=AGENT, metadata={"daimon_account": "other"})
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        (
            "GET",
            "/v1/sessions",
            200,
            {"data": [BODY, other.model_dump(mode="json")], "next_page": None},
        )
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        kwargs: dict[str, Any] = {"agent_id": "agent"}
        if owner == "tenant-filtered":
            kwargs["page"] = "opaque"
        expected = [
            sessions.SessionInfo.from_ma(s)
            async for s in before.beta.sessions.list(**kwargs)
            if s.metadata.get("daimon_account") == str(ACCOUNT)
        ]
        if owner == "agent":
            actual = await _list_agent_sessions_impl(runtime(after), AUTH)  # pyright: ignore[reportPrivateUsage]
        else:
            actual = await _list_tenant_sessions_impl(
                runtime(after), AUTH, "opaque", AGENT.name if owner == "tenant-filtered" else None
            )  # pyright: ignore[reportPrivateUsage]
        assert actual == expected
    assert host_guards
    same(old, new)


@pytest.mark.parametrize("bundle", [False, True])
async def test_public_start_turn_keeps_bundle_probe_send_and_clock(
    bundle: bool, host_guards: list[Scope], monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = ma_agent(id="agent", tenant_id=TENANT, metadata={"daimon_isolated": "true"})
    monkeypatch.setattr(agent_chat, "_resolve_ma_agent", AsyncMock(return_value=isolated))
    monkeypatch.setattr(agent_chat, "_resolve_environment_name", AsyncMock(return_value="env"))
    monkeypatch.setattr(
        agent_chat,
        "find_environment_by_daimon_tag",
        AsyncMock(return_value=ma_environment(id="env")),
    )
    monkeypatch.setattr(agent_chat, "_budget_channel", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_chat, "_bound_posture", AsyncMock(return_value=(frozenset(), False)))
    monkeypatch.setattr(agent_chat, "create_session", AsyncMock(return_value=SESSION))
    monkeypatch.setattr(agent_chat, "create_isolated_session", AsyncMock(return_value=SESSION))
    clock = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)
    replies = [("POST", "/v1/sessions/session/events", 200, ECHO)]
    if bundle:
        replies.insert(0, ("GET", "/v1/files/file", 200, {}))
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        token = None
        host = runtime(after)
        if bundle:
            host.settings.mcp.jwt_secret = SecretStr("n6-offline-test-key")
            token = bundle_handle.mint(
                "n6-offline-test-key",
                file_id="file",
                tenant_id=TENANT,
                agent_id=cast(uuid.UUID, AUTH.agent_id),
                sha256="a" * 64,
                now=dt.datetime.now(dt.UTC),
                ttl_days=7,
            )
            await before.beta.files.retrieve_metadata("file")
        await before.beta.sessions.events.send("session", events=cast(Any, MESSAGE))
        actual = await cast(Any, _start_turn_impl).__wrapped__(
            host, AUTH, 'hi "世界"\n', token, now=lambda: clock
        )  # pyright: ignore[reportFunctionMemberAccess, reportPrivateUsage]
        assert actual == {
            "handle": "session",
            "turn_event_id": "accepted",
            "turn_started_at": clock.isoformat(),
        }
    assert host_guards
    same(old, new)


async def test_public_continue_turn_keeps_ownership_recheck_send_and_clock(
    host_guards: list[Scope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        ("GET", "/v1/sessions/session", 200, BODY),
        ("POST", "/v1/sessions/session/events", 200, ECHO),
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        await before.beta.sessions.retrieve("session")
        await before.beta.sessions.events.send("session", events=cast(Any, MESSAGE))

        async def recheck() -> None:
            # Authorization runs after the ownership read and before sending.
            assert len(new.requests) == 1
            assert new.requests[0].method == "GET"

        actual = await cast(Any, _continue_turn_impl).__wrapped__(
            runtime(after), AUTH, "session", 'hi "世界"\n', now=lambda: clock, recheck=recheck
        )
        assert actual == {
            "handle": "session",
            "turn_event_id": "accepted",
            "turn_started_at": clock.isoformat(),
        }
    assert host_guards
    same(old, new)


@pytest.mark.parametrize("cursor", ["", "opaque-next"])
async def test_public_turn_cost_keeps_decimal_fold_filters_and_explicit_page_loop(
    cursor: str,
    host_guards: list[Scope],
) -> None:
    started = "2026-10-09T12:00:00+00:00"
    usage = ma_model_usage(input_tokens=1000, output_tokens=500)
    cost_event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="cost",
        model_request_start_id="start",
        model_usage=usage,
        processed_at=dt.datetime(2026, 10, 9, 12, 0, 1, tzinfo=dt.UTC),
        type="span.model_request_end",
    ).model_dump(mode="json")
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        ("GET", "/v1/sessions/session", 200, BODY),
        ("GET", "/v1/sessions/session/events", 200, {"data": [], "next_page": cursor}),
        (
            "GET",
            "/v1/sessions/session/events",
            200,
            {
                "data": [{"id": "accepted", "type": "user.message"}, cost_event],
                "next_page": None,
            },
        ),
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        await before.beta.sessions.retrieve("session")
        query: dict[str, Any] = {
            "created_at_gte": started,
            "types": ["span.model_request_end"],
            "order": "asc",
        }
        await before.beta.sessions.events.list("session", **query)
        await before.beta.sessions.events.list("session", **query, page=cursor)
        actual = await _get_turn_cost_impl(runtime(after), AUTH, "session", started, "accepted")
        cost = cost_of(usage, MODEL_PRICING[SESSION.agent.model.id])
        assert cost is not None
        assert actual == {
            "cost_usd": str(Decimal(str(cost)).quantize(Decimal("0.000001"))),
            "event_count": 1,
        }
    assert host_guards
    same(old, new)


@pytest.mark.parametrize("mode", ["own", "foreign", "unreadable"])
async def test_session_card_gate_keeps_one_read_and_refusal_copy(
    mode: str,
    host_guards: list[Scope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)
    origin = TurnOriginRow(
        id=uuid.UUID(int=3),
        tenant_id=TENANT,
        account_id=ACCOUNT,
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        responder_ma_agent_id="agent",
        responder_name="agent",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        created_at=clock,
        expires_at=clock,
    )
    live = ThreadSessionRow(
        id=uuid.UUID(int=4),
        tenant_id=TENANT,
        account_id=ACCOUNT,
        platform="discord",
        thread_id="thread",
        ma_session_id="session",
        watermark_message_id=None,
        status="live",
        created_at=clock,
        updated_at=clock,
    )
    monkeypatch.setattr(_session_gate, "get_live_thread_session", AsyncMock(return_value=live))

    def gated(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(_session_gate, "has_confirmation_gate", gated)
    body = (
        BODY
        if mode != "foreign"
        else ma_session(id="session", agent_id="other").model_dump(mode="json")
    )
    if mode == "unreadable":
        body = {"type": "error", "error": {"type": "not_found_error", "message": "gone"}}
    replies = [("GET", "/v1/sessions/session", 404 if mode == "unreadable" else 200, body)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        if mode == "unreadable":
            with pytest.raises(APIStatusError):
                await before.beta.sessions.retrieve("session")
        else:
            await before.beta.sessions.retrieve("session")
        actual = await _session_gate.session_card_gap(
            runtime(after), AUTH, origin, tool_name="publish"
        )
        assert (
            actual
            == {"own": None, "foreign": "other_agents_session", "unreadable": "session_unreadable"}[
                mode
            ]
        )
    assert host_guards
    same(old, new)


@pytest.mark.parametrize("operation", ["walk", "send", "events"])
@pytest.mark.parametrize("violation", ["scope", "ref", "grant"])
async def test_session_tool_scope_violations_stop_before_provider_io(
    operation: str,
    violation: str,
) -> None:
    transport = script([])
    async with transport.client() as client:
        kind, native_id = ("agent", "agent") if operation == "walk" else ("session", "session")
        backend = managed_agents(
            client,
            scope=SCOPE,
            resources=frozenset() if violation == "grant" else frozenset({(kind, native_id)}),
        )
        port = backend.extension(SessionTools, namespace="anthropic.session_tools", version=1)
        ref = resource_ref(backend, kind, native_id, scope=SCOPE)
        scope = SCOPE
        if violation == "scope":
            scope = SCOPE.model_copy(update={"tenant_id": "other"})
        elif violation == "ref":
            ref = ref.model_copy(update={"account_id": "other"})
        with pytest.raises(ScopeViolation):
            if operation == "walk":
                _ = [item async for item in port.walk(scope, ref)]
            elif operation == "send":
                await port.send(
                    scope, ref, SessionSend.model_validate({"events": MESSAGE}), key="key"
                )
            else:
                await port.list_events(scope, ref, EventQuery())
    assert transport.requests == []


async def test_session_send_does_not_cache_operation_keys_or_repeat_outcomes() -> None:
    replies = [("POST", "/v1/sessions/session/events", 200, ECHO)] * 2
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        backend = managed_agents(after, scope=SCOPE, resources=frozenset({("session", "session")}))
        port = backend.extension(SessionTools, namespace="anthropic.session_tools", version=1)
        for _ in range(2):
            await before.beta.sessions.events.send("session", events=cast(Any, MESSAGE))
            await port.send(
                SCOPE,
                resource_ref(backend, "session", "session", scope=SCOPE),
                SessionSend.model_validate({"events": MESSAGE}),
                key="same-key",
            )
    same(old, new)
