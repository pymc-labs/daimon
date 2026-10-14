"""Fail-closed Gemini MCP: actual SDK wire/DEBUG and owned loopback read tools."""

from __future__ import annotations

import copy
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from daimon.testing.asgi import INIT_BODY, INIT_HEADERS, parse_jsonrpc_response
from daimon.testing.db import build_test_engine
from daimon.testing.drivers.gemini.fake import MemoryStorage
from daimon.testing.drivers.gemini.live_cert import LimitedTransport
from daimon.testing.isolated_sdk import build_gemini_client
from daimon.testing.qa_mcp_host import QA_SESSION_ID, build_host, cleanup, schema_exists, serve
from google import genai
from mux.contracts.actions import UserMessage, UserToolResult
from mux.contracts.events import NativePart, TextPart, ToolResultPayload, ToolUsePayload
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, PageRequest, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, MCPConnection, Session, SessionSpec, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents, MCPSecretResolver
from mux.drivers.gemini.flash import FlashAttempt, FlashTransport
from mux.drivers.gemini.mcp_auth import authenticated_template
from mux.drivers.gemini.transport import Object, SDKTransport, Transport
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore, SimulatedCrash
from pydantic import JsonValue, TypeAdapter

SCOPE = Scope(tenant_id="qa", account_id="a", principal_id="p", authorization_id="auth")
THREAD = ThreadRef(
    channel=ChannelRef(tenant_id="qa", platform="headless", channel_id="c"), thread_id="th"
)
MESSAGE = UserMessage(content=(TextPart(text="describe the QA agent and list my sessions"),))
SECRET = "offline-gemini-bound-token"
LEAK = "unbound-sentinel-never-forward"
URL = "https://qa.invalid/mcp"
CONNECTION = MCPConnection(
    name="daimon-mcp",
    url=URL,
    credential_ref="host:owned",
    tool_policy={"allowed_tools": ["describe_agent", "list_my_sessions"]},
)
OBJECT = TypeAdapter[Object](Object)
TOOLS = (
    ToolSpec(name="code_execution", kind="builtin"),
    ToolSpec(name="google_search", kind="builtin"),
    ToolSpec(name="url_context", kind="builtin"),
    ToolSpec(name="echo", kind="custom", input_schema={"type": "object", "properties": {}}),
)


class Resolver:
    def __init__(self, *, rotate: bool = False, fault: str | None = None) -> None:
        self.calls: list[tuple[Scope, str, str]] = []
        self.rotate, self.fault, self.secret = rotate, fault, SECRET

    async def resolve(self, scope: Scope, reference: str, destination: str) -> str:
        self.calls.append((scope, reference, destination))
        if (scope, reference, destination) != (SCOPE, "host:owned", URL) or self.fault == "revoked":
            raise RuntimeError("private resolver failure " + self.secret)
        if self.fault == "blank":
            return ""
        if self.fault == "newline":
            return "bad\ncredential"
        return self.secret + (f"-{len(self.calls)}" if self.rotate else "")


def interaction(
    identity: str, *, status: str = "completed", steps: list[JsonValue] | None = None
) -> Object:
    stamp = datetime.now(UTC).isoformat()
    return {
        "id": identity,
        "created": stamp,
        "updated": stamp,
        "status": status,
        "environment_id": "owned-environment",
        "steps": steps or [],
        "usage": {"total_input_tokens": 2, "total_output_tokens": 1},
    }


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.saved: dict[str, Object] = {}
        self.statuses: list[int] = []
        self.function_action = False
        self.counter = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=self.saved[request.url.path.rsplit("/", 1)[-1]])
        assert request.method == "POST" and request.url.path == "/v1beta/interactions"
        body = OBJECT.validate_json(request.content)
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            return httpx.Response(status, json={"error": {"message": SECRET, "code": status}})
        self.counter += 1
        identity = f"interaction-{self.counter}"
        steps: list[JsonValue] = []
        native_status = "completed"
        if self.function_action and self.counter == 1:
            native_status = "requires_action"
            steps.append(
                {"type": "function_call", "id": "function-1", "name": "echo", "arguments": {}}
            )
        else:
            tools = body.get("tools", [])
            assert isinstance(tools, list)
            for tool in tools:
                if isinstance(tool, dict) and tool.get("type") == "mcp_server":
                    steps.extend(
                        [
                            {
                                "type": "mcp_server_tool_call",
                                "id": identity + ":call",
                                "name": "describe_agent",
                                "server_name": "daimon-mcp",
                                "arguments": {},
                            },
                            {
                                "type": "mcp_server_tool_result",
                                "id": identity + ":result",
                                "call_id": identity + ":call",
                                "result": {
                                    "content": [{"type": "text", "text": "qa-agent"}],
                                    "isError": False,
                                },
                                "is_error": False,
                            },
                        ]
                    )
        raw = interaction(identity, status=native_status, steps=steps)
        self.saved[identity] = raw
        return httpx.Response(200, json=raw)


@asynccontextmanager
async def mcp_sdk(transport: httpx.MockTransport) -> AsyncIterator[genai.Client]:
    client = build_gemini_client(
        transport=transport,
        api_key="offline-placeholder",
        base_url="https://offline.invalid",
        api_version="v1beta",
        headers={},
    )
    try:
        yield client
    finally:
        try:
            await client.aio.aclose()
        finally:
            client.close()


@pytest.fixture
async def sdk_wire() -> AsyncIterator[tuple[SDKTransport, Wire]]:
    wire = Wire()
    async with mcp_sdk(httpx.MockTransport(wire.handle)) as client:
        yield SDKTransport(client), wire


async def gemini_auth_session(
    transport: Transport,
    resolver: MCPSecretResolver | None,
    *,
    authenticated: bool = True,
    connection: MCPConnection = CONNECTION,
    tools: tuple[ToolSpec, ...] = TOOLS,
) -> tuple[GeminiManagedAgents, MemoryStorage, MemoryStateStore, Session]:
    storage, state = MemoryStorage(), MemoryStateStore()
    driver = GeminiManagedAgents(
        transport,
        storage=storage,
        state_store=state,
        account_scope_id="project",
        mcp_secrets=resolver,
    )
    agent = await driver.agents.create(
        SCOPE,
        AgentSpec(
            name="qa",
            model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
            tools=tools,
            mcp_servers=(connection,) if authenticated else None,
        ),
        key="agent",
    )
    session = await driver.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={"thread": THREAD.model_dump(mode="json"), "binding_id": "binding"},
                )
            },
        ),
        key="session",
    )
    return driver, storage, state, session


@pytest.mark.asyncio
async def test_authenticated_follow_up_refreshes_each_sdk_dispatch_without_persisting_or_logging(
    sdk_wire: tuple[SDKTransport, Wire], caplog: pytest.LogCaptureFixture
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver(rotate=True)
    caplog.set_level(logging.DEBUG)
    driver, storage, state, session = await gemini_auth_session(sdk, resolver.resolve)
    first = await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="first")
    restarted = GeminiManagedAgents(
        sdk,
        storage=storage,
        state_store=state,
        account_scope_id="project",
        mcp_secrets=resolver.resolve,
    )
    second = await restarted.events.send(SCOPE, session.ref, (MESSAGE,), key="second")
    assert first.status == second.status == "queued" and first.turn_id != second.turn_id
    posts = [OBJECT.validate_json(r.content) for r in wire.requests if r.method == "POST"]
    assert len(posts) == 2 and len(resolver.calls) == 4
    for n, post in enumerate(posts):
        tools = post["tools"]
        assert isinstance(tools, list)
        assert tools[:3] == [
            {"type": name} for name in ("code_execution", "google_search", "url_context")
        ]
        assert isinstance(tools[-1], dict)
        assert tools[-1] == {
            "type": "mcp_server",
            "name": "daimon-mcp",
            "url": URL,
            "allowed_tools": [{"mode": "auto", "tools": ["describe_agent", "list_my_sessions"]}],
            "headers": {"Authorization": f"Bearer {SECRET}-{(n + 1) * 2}"},
        }
        assert json.dumps(post).count(SECRET) == 1
    assert posts[1]["previous_interaction_id"] == "interaction-1"
    assert posts[1]["environment"] == "owned-environment"
    count = len(wire.requests)
    assert await restarted.events.send(SCOPE, session.ref, (MESSAGE,), key="second") == second
    assert len(wire.requests) == count and len(resolver.calls) == 4
    async with storage.transaction() as records:
        assert SECRET not in repr(records)
        assert "headers" not in json.dumps(records.sessions[session.ref.id].request)
    assert SECRET not in caplog.text
    assert SECRET not in repr(await state.get_operation(SCOPE, "first"))


BAD_TOOLS: tuple[Object, ...] = (
    {"type": "MCP", "headers": {"Authorization": LEAK}},
    {"type": "Mcp", "url": "https://user:pass@foreign.invalid/mcp"},
    {"type": "mcp_server ", "headers": {"Authorization": LEAK}},
    {"type": "remote_mcp", "authorization": LEAK},
    {
        "type": "function",
        "name": "f",
        "description": "",
        "parameters": {},
        "headers": {"Authorization": LEAK},
    },
    {"type": "google_search", "authorization": LEAK},
    {"type": "mcp_server", "name": "unbound", "url": "https://foreign.invalid/mcp"},
    {"type": "mcp_server", "name": "unbound", "url": URL, "headers": {"Authorization": LEAK}},
    {"type": "unknown", "headers": {"Authorization": LEAK}},
)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", BAD_TOOLS)
@pytest.mark.parametrize("position", (0, 1))
@pytest.mark.parametrize("authenticated", (False, True))
@pytest.mark.parametrize("follow_up", (False, True))
async def test_every_unbound_adjacent_carrier_refuses_before_history_resolver_post_or_claim(
    sdk_wire: tuple[SDKTransport, Wire],
    tool: Object,
    position: int,
    authenticated: bool,
    follow_up: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver()
    driver, storage, state, session = await gemini_auth_session(
        sdk, resolver.resolve, authenticated=authenticated
    )
    if follow_up:
        await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="first")
    wire.requests.clear()
    resolver.calls.clear()
    caplog.set_level(logging.DEBUG)
    async with storage.transaction() as records:
        tools = records.sessions[session.ref.id].request["tools"]
        assert isinstance(tools, list)
        tools.insert(position, copy.deepcopy(tool))
    with pytest.raises(UnsupportedCapability):
        await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="blocked")
    assert not wire.requests and not resolver.calls
    assert await state.get_operation(SCOPE, "blocked") is None
    assert LEAK not in caplog.text and SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    (
        "destination",
        "policy",
        "top_headers",
        "environment",
        "no_resolver",
        "revoked",
        "blank",
        "newline",
        "foreign_tenant",
        "foreign_principal",
    ),
)
async def test_auth_intent_and_resolver_faults_are_definite_pre_io_refusals(
    sdk_wire: tuple[SDKTransport, Wire], fault: str, caplog: pytest.LogCaptureFixture
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver()
    driver, storage, state, session = await gemini_auth_session(sdk, resolver.resolve)
    await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="first")
    wire.requests.clear()
    resolver.calls.clear()
    caplog.set_level(logging.DEBUG)
    scope = SCOPE
    if fault in ("revoked", "blank", "newline"):
        resolver.fault = fault
    elif fault.startswith("foreign_"):
        scope = SCOPE.model_copy(
            update={"tenant_id" if fault == "foreign_tenant" else "principal_id": "foreign"}
        )
    elif fault == "no_resolver":
        driver = GeminiManagedAgents(
            sdk, storage=storage, state_store=state, account_scope_id="project"
        )
    else:
        async with storage.transaction() as records:
            request = records.sessions[session.ref.id].request
            tools = request["tools"]
            assert isinstance(tools, list) and isinstance(tools[-1], dict)
            if fault == "destination":
                tools[-1]["url"] = "https://foreign.invalid/mcp"
            elif fault == "policy":
                tools[-1]["allowed_tools"] = [{"mode": "auto", "tools": ["start_turn"]}]
            elif fault == "top_headers":
                request["headers"] = {"Authorization": LEAK}
            else:
                request["environment"] = {"type": "remote", "headers": {"Authorization": LEAK}}
    with pytest.raises((UnsupportedCapability, ProviderError, ScopeViolation)):
        await driver.events.send(scope, session.ref, (MESSAGE,), key="blocked")
    assert not wire.requests
    assert len(resolver.calls) == (
        1 if fault in ("revoked", "blank", "newline", "foreign_principal") else 0
    )
    assert await state.get_operation(scope, "blocked") is None
    assert LEAK not in caplog.text and SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    (
        "http://qa.invalid/mcp",
        "https://u:p@qa.invalid/mcp",
        "https://@qa.invalid/mcp",
        "https://qa.invalid/mcp?token=x",
        "https://qa.invalid/mcp#x",
        "https://q%61.invalid/mcp",
        "https://qa.invalid:0/mcp",
        "https://qa.invalid:70000/mcp",
        "https://qa.invalid\\foreign/mcp",
        "https://qa.invalid/ mcp",
        "HTTPS://qa.invalid/mcp",
    ),
)
async def test_bound_destinations_refuse_before_any_provider_or_secret_io(
    sdk_wire: tuple[SDKTransport, Wire], url: str
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver()
    with pytest.raises(UnsupportedCapability):
        await gemini_auth_session(
            sdk, resolver.resolve, connection=CONNECTION.model_copy(update={"url": url})
        )
    assert not wire.requests and not resolver.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver_enabled", (False, True))
@pytest.mark.parametrize(
    "url",
    (
        f"https://u:{LEAK}@foreign.invalid/mcp",
        f"https://foreign.invalid/mcp?token={LEAK}",
        f"https://foreign.invalid/mcp#{LEAK}",
        "http://qa.invalid/mcp",
        "https://q%61.invalid/mcp",
        "https://qa.invalid\\foreign/mcp",
        "https://qa.invalid/ mcp",
    ),
)
async def test_anonymous_destinations_refuse_before_persistence_wire_and_debug(
    sdk_wire: tuple[SDKTransport, Wire],
    resolver_enabled: bool,
    url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sdk, wire = sdk_wire
    resolver, storage = Resolver(), MemoryStorage()
    driver = GeminiManagedAgents(
        sdk,
        storage=storage,
        state_store=MemoryStateStore(),
        account_scope_id="project",
        mcp_secrets=resolver.resolve if resolver_enabled else None,
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(UnsupportedCapability) as caught:
        await driver.agents.create(
            SCOPE,
            AgentSpec(
                name="anonymous",
                model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
                mcp_servers=(MCPConnection(name="public", url=url),),
            ),
            key="refuse",
        )
    assert caught.value.missing == ("https_mcp_destination",)
    async with storage.transaction() as records:
        assert records.agents == {}
    assert wire.requests == [] and resolver.calls == []
    assert LEAK not in caplog.text and LEAK not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver_enabled", (False, True))
async def test_clean_anonymous_https_retains_native_shape_without_resolution(
    sdk_wire: tuple[SDKTransport, Wire],
    resolver_enabled: bool,
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver()
    driver, _, _, session = await gemini_auth_session(
        sdk,
        resolver.resolve if resolver_enabled else None,
        connection=MCPConnection(name="public", url=URL),
    )
    for key in ("one", "two"):
        assert (
            await driver.events.send(SCOPE, session.ref, (MESSAGE,), key=key)
        ).status == "queued"
    posts = [OBJECT.validate_json(r.content) for r in wire.requests if r.method == "POST"]
    assert len(posts) == 2 and resolver.calls == []
    assert all(
        isinstance(tools := body["tools"], list)
        and tools[-1] == {"type": "mcp_server", "name": "public", "url": URL}
        for body in posts
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    (
        {},
        {"allowed_tools": []},
        {"allowed_tools": ["*"]},
        {"allowed_tools": ["describe_agent", "describe_agent"]},
        {"allowed_tools": ["describe_agent"], "required": True},
        {"allowed_tools": ["describe_agent"], "headers": {"Authorization": LEAK}},
    ),
)
async def test_bound_policy_is_closed_and_never_silently_dropped(
    sdk_wire: tuple[SDKTransport, Wire], policy: Object
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver()
    with pytest.raises(UnsupportedCapability):
        await gemini_auth_session(
            sdk, resolver.resolve, connection=CONNECTION.model_copy(update={"tool_policy": policy})
        )
    assert not wire.requests and not resolver.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_second", (False, True))
async def test_exactly_one_server_not_one_bound_plus_anonymous(
    sdk_wire: tuple[SDKTransport, Wire], bound_second: bool
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver()
    driver = GeminiManagedAgents(
        sdk,
        storage=MemoryStorage(),
        state_store=MemoryStateStore(),
        account_scope_id="project",
        mcp_secrets=resolver.resolve,
    )
    second = MCPConnection(
        name="second",
        url="https://second.invalid/mcp",
        credential_ref="host:second" if bound_second else None,
        tool_policy={"allowed_tools": ["x"]} if bound_second else {},
    )
    with pytest.raises(UnsupportedCapability):
        await driver.agents.create(
            SCOPE,
            AgentSpec(
                name="bad",
                model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
                mcp_servers=(CONNECTION, second),
            ),
            key="bad",
        )
    assert not wire.requests and not resolver.calls


@pytest.mark.asyncio
async def test_explicit_empty_tools_remain_valid_without_mcp(
    sdk_wire: tuple[SDKTransport, Wire],
) -> None:
    sdk, wire = sdk_wire
    driver, _, _, session = await gemini_auth_session(sdk, None, authenticated=False, tools=())
    await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="empty")
    post = OBJECT.validate_json(wire.requests[0].content)
    assert post["tools"] == []


@pytest.mark.asyncio
async def test_function_result_uses_same_root_and_fresh_authentication(
    sdk_wire: tuple[SDKTransport, Wire],
) -> None:
    sdk, wire = sdk_wire
    wire.function_action = True
    resolver = Resolver(rotate=True)
    driver, storage, _, session = await gemini_auth_session(sdk, resolver.resolve)
    first = await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="first")
    async with storage.transaction() as records:
        action = records.sessions[session.ref.id].session.required_actions[0]
    second = await driver.events.send(
        SCOPE,
        session.ref,
        (UserToolResult(action_id=action.id, content=(TextPart(text="ok"),)),),
        key="result",
    )
    assert second.turn_id == first.turn_id and len(resolver.calls) == 4
    posts = [OBJECT.validate_json(r.content) for r in wire.requests if r.method == "POST"]
    assert posts[-1]["input"] == [
        {"type": "function_result", "name": "echo", "call_id": "function-1", "result": "ok"}
    ]


class Accounting:
    def __init__(self) -> None:
        self.receipts: list[FlashAttempt] = []

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
        assert model.provider == "gemini" and attempt >= 1

    async def after_attempt(self, *, receipt: FlashAttempt) -> None:
        self.receipts.append(receipt)


@pytest.mark.asyncio
async def test_each_flash_fallback_sdk_post_resolves_fresh_without_altering_policy(
    sdk_wire: tuple[SDKTransport, Wire], caplog: pytest.LogCaptureFixture
) -> None:
    sdk, wire = sdk_wire
    wire.statuses = [503, 503, 200]
    resolver, accounting = Resolver(rotate=True), Accounting()
    transport = FlashTransport(sdk, accounting)
    driver, _, _, session = await gemini_auth_session(transport, resolver.resolve)
    caplog.set_level(logging.DEBUG)
    receipt = await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="fallback")
    assert receipt.status == "queued" and len(resolver.calls) == 4
    assert [r.outcome for r in accounting.receipts] == ["refused", "refused", "accepted"]
    posts = [OBJECT.validate_json(r.content) for r in wire.requests]
    assert [OBJECT.validate_python(p["agent_config"])["model"] for p in posts] == [
        "gemini-3.8-flash",
        "gemini-flash-latest",
        "gemini-3.5-flash-lite",
    ]
    for n, post in enumerate(posts, start=2):
        assert json.dumps(post).count(f"{SECRET}-{n}") == 1
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (401, 403, 429, 503))
async def test_sdk_errors_do_not_log_the_request_or_echoed_secret(
    sdk_wire: tuple[SDKTransport, Wire], status: int, caplog: pytest.LogCaptureFixture
) -> None:
    sdk, wire = sdk_wire
    wire.statuses = [status]
    resolver = Resolver()
    driver, _, _, session = await gemini_auth_session(sdk, resolver.resolve)
    caplog.set_level(logging.DEBUG)
    if status == 503:
        receipt = await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="error")
        assert receipt.status == "outcome_unknown"
    else:
        with pytest.raises(ProviderError) as caught:
            await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="error")
        assert SECRET not in repr(caught.value) and caught.value.__cause__ is None
    assert len(wire.requests) == 1 and SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", (True, False))
async def test_restored_acknowledgement_rechecks_auth_before_history_without_second_post(
    sdk_wire: tuple[SDKTransport, Wire],
    revoked: bool,
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver()
    _, storage, state, session = await gemini_auth_session(sdk, resolver.resolve)
    crashed = GeminiManagedAgents(
        sdk,
        storage=storage,
        state_store=state.restart(crash={"advance_operation": "after_commit"}),
        account_scope_id="project",
        mcp_secrets=resolver.resolve,
    )
    with pytest.raises(SimulatedCrash):
        await crashed.events.send(SCOPE, session.ref, (MESSAGE,), key="restore")
    wire.requests.clear()
    resolver.calls.clear()
    resolver.fault = "revoked" if revoked else None
    restarted = GeminiManagedAgents(
        sdk,
        storage=storage,
        state_store=state.restart(),
        account_scope_id="project",
        mcp_secrets=resolver.resolve,
    )
    if revoked:
        with pytest.raises(ProviderError, match="mcp_secret_unavailable"):
            await restarted.events.send(SCOPE, session.ref, (MESSAGE,), key="restore")
        assert wire.requests == []
    else:
        receipt = await restarted.events.send(SCOPE, session.ref, (MESSAGE,), key="restore")
        assert receipt.status == "queued" and receipt.input_ids == ("interaction-1",)
        assert [r.method for r in wire.requests] == ["GET"]
    assert len(resolver.calls) == 1


@pytest.mark.asyncio
async def test_actual_resolved_secret_cannot_also_appear_in_input_before_claim(
    sdk_wire: tuple[SDKTransport, Wire],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver()
    driver, _, state, session = await gemini_auth_session(sdk, resolver.resolve)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ProviderError, match="mcp_secret_outside_binding"):
        await driver.events.send(
            SCOPE,
            session.ref,
            (UserMessage(content=(TextPart(text=SECRET),)),),
            key="duplicate",
        )
    assert wire.requests == [] and len(resolver.calls) == 1
    assert await state.get_operation(SCOPE, "duplicate") is None
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", (None, 42, "with space", "with\tcontrol", "nonascii-é"))
async def test_invalid_resolver_output_refuses_before_native_io(
    sdk_wire: tuple[SDKTransport, Wire],
    bad: object,
) -> None:
    sdk, wire = sdk_wire

    async def wrong(scope: Scope, reference: str, destination: str) -> object:
        assert (scope, reference, destination) == (SCOPE, "host:owned", URL)
        return bad

    driver, _, state, session = await gemini_auth_session(sdk, cast(MCPSecretResolver, wrong))
    with pytest.raises(ProviderError, match="mcp_secret_unavailable"):
        await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="invalid")
    assert wire.requests == [] and await state.get_operation(SCOPE, "invalid") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    (
        "json_roundtrip",
        "headers",
        "second_server",
        "destination",
        "builtin_auth",
        "variant",
        "move_marker",
        "top_headers",
        "duplicate_marker",
        "move_to_existing_function",
        "environment_carrier",
        "configuration_carrier",
        "changed_input",
        "changed_instruction",
    ),
)
async def test_sdk_boundary_refuses_unbound_or_changed_auth_intent_before_resolver(
    sdk_wire: tuple[SDKTransport, Wire],
    fault: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sdk, wire = sdk_wire
    resolver = Resolver()
    _, storage, _, session = await gemini_auth_session(sdk, resolver.resolve)
    async with storage.transaction() as records:
        original = records.sessions[session.ref.id].request
        request = await authenticated_template(
            SCOPE,
            original,
            original,
            (CONNECTION,),
            resolver.resolve,
            overrides={
                "input": [{"type": "text", "text": "owned"}],
                "background": True,
                "store": True,
            },
        )
    resolver.calls.clear()
    tools = request["tools"]
    assert isinstance(tools, list) and isinstance(tools[-1], dict)
    mcp = tools[-1]
    if fault == "json_roundtrip":
        request = OBJECT.validate_json(json.dumps(request))
    elif fault == "headers":
        headers = mcp["headers"]
        assert isinstance(headers, dict)
        headers["X-Api-Key"] = LEAK
    elif fault == "second_server":
        tools.insert(
            0, {"type": "mcp_server", "name": "other", "url": "https://foreign.invalid/mcp"}
        )
    elif fault == "destination":
        mcp["url"] = "https://foreign.invalid/mcp"
    elif fault == "builtin_auth":
        tools.insert(0, {"type": "google_search", "authorization": LEAK})
    elif fault == "variant":
        mcp["type"] = "MCP_SERVER"
    elif fault == "move_to_existing_function":
        function = next(t for t in tools if isinstance(t, dict) and t.get("type") == "function")
        function["headers"] = mcp.pop("headers")
    elif fault == "move_marker":
        tools.insert(0, {"type": "code_execution", "headers": mcp.pop("headers")})
    elif fault == "top_headers":
        request["headers"] = {"Authorization": LEAK}
    elif fault == "environment_carrier":
        request["environment"] = {"type": "remote", "headers": {"Authorization": LEAK}}
    elif fault == "configuration_carrier":
        configuration = request["agent_config"]
        assert isinstance(configuration, dict)
        configuration["headers"] = {"Authorization": LEAK}
    elif fault == "changed_input":
        request["input"] = [{"type": "text", "text": LEAK}]
    elif fault == "changed_instruction":
        request["system_instruction"] = LEAK
    else:
        tools.insert(0, copy.deepcopy(mcp))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(UnsupportedCapability):
        await sdk.create(request)
    assert resolver.calls == [] and wire.requests == []
    assert SECRET not in caplog.text and LEAK not in caplog.text


@pytest.mark.asyncio
async def test_real_loopback_read_pair_uses_only_the_owned_bound_bearer_and_cleans_up(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not database_url:
        pytest.skip("isolated lane test DB is required")
    qa = await build_host(database_url=database_url, port=0, root=tmp_path)
    manifest_path = qa.manifest.path(tmp_path)
    resolutions: list[tuple[Scope, str, str]] = []
    try:
        async with serve(qa) as loopback, httpx.AsyncClient(timeout=30, trust_env=False) as host:

            async def resolve(scope: Scope, reference: str, destination: str) -> str:
                resolutions.append((scope, reference, destination))
                if (scope, reference, destination) != (
                    SCOPE,
                    "host:owned",
                    URL,
                ) or not qa.bearer.accepts(qa.bearer.token, datetime.now(UTC)):
                    raise RuntimeError("unavailable owned QA bearer")
                return qa.bearer.token

            requests: list[httpx.Request] = []
            results: dict[str, Object] = {}
            saved: Object | None = None

            async def bridge(request: httpx.Request) -> httpx.Response:
                nonlocal saved
                requests.append(request)
                if request.method == "GET":
                    assert saved is not None
                    return httpx.Response(200, json=saved)
                body = OBJECT.validate_json(request.content)
                tools = body["tools"]
                assert isinstance(tools, list)
                servers = [
                    t for t in tools if isinstance(t, dict) and t.get("type") == "mcp_server"
                ]
                assert len(servers) == 1
                server = servers[0]
                assert isinstance(server, dict) and server["url"] == URL
                headers = OBJECT.validate_python(server["headers"])
                authorization = headers["Authorization"]
                assert (
                    isinstance(authorization, str) and authorization == "Bearer " + qa.bearer.token
                )
                assert request.content.count(qa.bearer.token.encode()) == 1
                assert server["allowed_tools"] == [
                    {"mode": "auto", "tools": ["describe_agent", "list_my_sessions"]}
                ]

                async def rpc(payload: Object) -> Object:
                    response = await host.post(
                        loopback,
                        json=payload,
                        headers={**INIT_HEADERS, "Authorization": authorization},
                    )
                    assert response.status_code == 200
                    return OBJECT.validate_python(parse_jsonrpc_response(response))

                await rpc(OBJECT.validate_python(INIT_BODY))
                discovered = await rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                discovery = OBJECT.validate_python(discovered["result"])["tools"]
                assert isinstance(discovery, list)
                names = TypeAdapter[list[str]](list[str]).validate_python(
                    [OBJECT.validate_python(t)["name"] for t in discovery],
                    strict=True,
                )
                assert set(names) == {"describe_agent", "list_my_sessions"}
                steps: list[JsonValue] = []
                for n, name in enumerate(("describe_agent", "list_my_sessions"), start=3):
                    reply = await rpc(
                        {
                            "jsonrpc": "2.0",
                            "id": n,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": {}},
                        }
                    )
                    assert "error" not in reply
                    result = OBJECT.validate_python(reply["result"])
                    results[name] = result
                    steps.extend(
                        [
                            {
                                "type": "mcp_server_tool_call",
                                "id": name,
                                "name": name,
                                "server_name": "daimon-mcp",
                                "arguments": {},
                            },
                            {
                                "type": "mcp_server_tool_result",
                                "id": name + ":result",
                                "call_id": name,
                                "result": result,
                                "is_error": result.get("isError", False),
                            },
                        ]
                    )
                saved = interaction("interaction-loopback", steps=steps)
                return httpx.Response(200, json=saved)

            async with mcp_sdk(httpx.MockTransport(bridge)) as client:
                caplog.set_level(logging.DEBUG)
                driver, storage, state, session = await gemini_auth_session(
                    SDKTransport(client), resolve
                )
                receipt = await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="pair")
                assert receipt.status == "queued" and len(resolutions) == 2
                page = await driver.events.list(SCOPE, session.ref, page=PageRequest())
                payloads = [e.typed_payload() for e in page.data]
                calls = [p for p in payloads if isinstance(p, ToolUsePayload)]
                assert {p.tool_name for p in calls} == {"describe_agent", "list_my_sessions"}
                assert all(p.executor == "mcp" and p.mcp_server == "daimon-mcp" for p in calls)
                observed = [p for p in payloads if isinstance(p, ToolResultPayload)]
                assert len(observed) == 2
                for result in observed:
                    assert result.is_error is False and isinstance(result.content[0], NativePart)
                    assert result.content[0].payload["result"] == results[result.call_id]
                assert "qa-agent" in json.dumps(results["describe_agent"])
                assert QA_SESSION_ID in json.dumps(results["list_my_sessions"])
                for header in ({}, {"Authorization": "Bearer wrong-owned-bearer"}):
                    response = await host.post(
                        loopback, json=INIT_BODY, headers={**INIT_HEADERS, **header}
                    )
                    assert response.status_code == 401
                qa.bearer.revoke()
                response = await host.post(
                    loopback,
                    json=INIT_BODY,
                    headers={**INIT_HEADERS, "Authorization": "Bearer " + qa.bearer.token},
                )
                assert response.status_code == 401
                requests.clear()
                with pytest.raises(ProviderError, match="mcp_secret_unavailable"):
                    await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="revoked")
                assert requests == [] and await state.get_operation(SCOPE, "revoked") is None
                async with storage.transaction() as records:
                    assert qa.bearer.token not in repr(records)
                assert qa.bearer.token not in caplog.text
        assert qa.manifest.refused_egress == []
    finally:
        await qa.engine.dispose()
        cleaned = await cleanup(manifest_path, database_url=database_url)
    assert cleaned.status == "cleaned"
    probe = build_test_engine(database_url, "public")
    try:
        assert not await schema_exists(probe, qa.manifest.schema)
    finally:
        await probe.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    (
        {"type": "mcp_server", "name": "unbound", "url": URL, "headers": {"Authorization": LEAK}},
        {"type": "mcp_server", "name": "public", "url": f"https://u:{LEAK}@foreign.invalid/mcp"},
        {
            "type": "mcp_server",
            "name": "public",
            "url": f"https://foreign.invalid/mcp?token={LEAK}",
        },
        {"type": "mcp_server", "name": "public", "url": f"https://foreign.invalid/mcp#{LEAK}"},
        {"type": "mcp_server", "name": "public", "url": "http://qa.invalid/mcp"},
        {"type": "mcp_server", "name": "public", "url": "https://q%61.invalid/mcp"},
        {"type": "mcp_server", "name": "public", "url": "https://qa.invalid\\foreign/mcp"},
        {"type": "mcp_server", "name": "public", "url": "https://qa.invalid/ mcp"},
        {"type": "mcp_server", "name": "with space", "url": URL},
        {"type": "mcp_server", "name": 1, "url": URL},
        {"type": "MCP", "authorization": LEAK},
        {"type": "google_search", "authorization": LEAK},
        {"type": "function", "name": {"authorization": LEAK}, "description": "", "parameters": {}},
        {"type": "function", "name": "valid", "description": {"headers": LEAK}, "parameters": {}},
        {"type": "function", "name": "valid", "description": "", "parameters": []},
    ),
)
async def test_unmarked_native_tools_cannot_smuggle_credentials_or_invalid_shapes(
    sdk_wire: tuple[SDKTransport, Wire],
    tool: Object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sdk, wire = sdk_wire
    caplog.set_level(logging.DEBUG)
    with pytest.raises(UnsupportedCapability):
        await sdk.create(
            {"agent": "deep-research-pro-preview-12-2025", "input": [], "tools": [tool]}
        )
    assert wire.requests == [] and LEAK not in caplog.text


@pytest.mark.asyncio
async def test_unmarked_sdk_request_refuses_unknown_top_level_credential_carriers(
    sdk_wire: tuple[SDKTransport, Wire],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sdk, wire = sdk_wire
    caplog.set_level(logging.DEBUG)
    with pytest.raises(UnsupportedCapability):
        await sdk.create(
            {
                "agent": "deep-research-pro-preview-12-2025",
                "input": [],
                "tools": [],
                "headers": {"Authorization": LEAK},
            }
        )
    assert wire.requests == [] and LEAK not in caplog.text


@pytest.mark.asyncio
async def test_authenticated_payload_preserves_existing_token_limit_and_flash_controls(
    sdk_wire: tuple[SDKTransport, Wire],
) -> None:
    sdk, wire = sdk_wire
    wire.statuses = [503, 200]
    resolver, accounting = Resolver(rotate=True), Accounting()
    limited = LimitedTransport(FlashTransport(sdk, accounting), max_total_tokens=320)
    driver, _, _, session = await gemini_auth_session(limited, resolver.resolve)
    receipt = await driver.events.send(SCOPE, session.ref, (MESSAGE,), key="bounded")
    assert receipt.status == "queued" and len(resolver.calls) == 3
    bodies = [OBJECT.validate_json(r.content) for r in wire.requests]
    configurations = [OBJECT.validate_python(b["agent_config"]) for b in bodies]
    assert [c["model"] for c in configurations] == ["gemini-3.8-flash", "gemini-flash-latest"]
    assert [c["max_total_tokens"] for c in configurations] == [320, 320]
    assert [r.outcome for r in accounting.receipts] == ["refused", "accepted"]


class NamesOnly(Mapping[str, str]):
    def __init__(self, name: str) -> None:
        self.name = name

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        if key != self.name:
            raise KeyError(key)
        raise AssertionError("Gemini MCP test client read an ambient SDK value")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "OPENAI_CUSTOM_HEADERS",
        "aNtHrOpIc_FUTURE_SETTING",
    ),
)
async def test_both_gemini_mcp_test_clients_refuse_names_before_http_allocation(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Gemini MCP test allocated HTTP before ambient refusal")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", NamesOnly(name))
        scoped.setattr(httpx, "Client", forbidden)
        scoped.setattr(httpx, "AsyncClient", forbidden)
        with pytest.raises(ValueError, match="refuses ambient"):
            async with mcp_sdk(httpx.MockTransport(Wire().handle)):
                raise AssertionError("Gemini MCP test admitted ambient options")
