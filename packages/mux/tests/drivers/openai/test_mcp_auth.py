"""Per-session authenticated MCP via actual SDK; all credentials are fictional."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from copy import deepcopy
from typing import cast

import httpx
import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Revision, Scope, SkillRef
from mux.contracts.resources import AgentSpec, MCPConnection, SessionSpec, ToolSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai._common import Context, objects
from mux.drivers.openai.agents import agent_body, agent_spec
from mux.drivers.openai.mcp_auth import KEY, MCPSecretResolver
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.skill_bindings import decode
from mux.drivers.openai.transport import Object, SDKTransport, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.testing.isolated_sdk import build_async_openai
from openai import AsyncOpenAI
from pydantic import JsonValue

from .conftest import SCOPE, native_session

URL = "https://qa.example.com/mcp"


@pytest.fixture(autouse=True)
def offline_openai_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructors must not inherit operator URL, org, project or signing keys.
    for name in tuple(os.environ):
        if name.startswith("OPENAI_"):
            monkeypatch.delenv(name)


def spec(connection: MCPConnection | None = None) -> AgentSpec:
    return AgentSpec(
        name="fixture",
        model=ModelRef(provider="openai", id="gpt-6-luna"),
        tools=(ToolSpec(name="web_search", kind="builtin"),),
        mcp_servers=(
            connection
            or MCPConnection(
                name="daimon-mcp",
                url=URL,
                credential_ref="host:owned-token",
                tool_policy={
                    "allowed_tools": ["describe_agent", "list_my_sessions"],
                    "required": True,
                },
            ),
        ),
    )


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.agent: Object = {}
        self.resolve_calls: list[tuple[object, str, str]] = []
        self.fail: str | None = None
        self.echo_defaults = False
        self.secret = "fictional-per-user-bearer"
        self.vaults: dict[str, Object] = {}
        self.credentials: dict[str, Object] = {}
        self.sessions: dict[str, Object] = {}
        self.vault_count = 0

    async def resolve(self, scope: object, ref: str, destination: str) -> str:
        self.resolve_calls.append((scope, ref, destination))
        if self.fail == "secret_failure":
            raise RuntimeError("fictional-private-error")
        assert scope == SCOPE and ref == "host:owned-token" and destination == URL
        return self.secret

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/vaults":
            if request.method == "GET":
                return httpx.Response(
                    200, json={"data": list(self.vaults.values()), "has_more": False}
                )
            self.vault_count += 1
            identity = "vault-" + str(self.vault_count)
            raw = {"id": identity, **json.loads(request.content)}
            self.vaults[identity] = raw
            return httpx.Response(200, json=raw)
        if path.startswith("/v1/vaults/"):
            identity = path.split("/")[3]
            if identity not in self.vaults:
                return httpx.Response(404, json={"error": {"message": "missing"}})
            if path.endswith("/credentials"):
                raw = {
                    "id": "credential-" + identity,
                    "vault_id": identity,
                    "created_at": 0,
                    **json.loads(request.content),
                }
                self.credentials[identity] = raw
                return httpx.Response(200, json=raw)
            if request.method == "DELETE":
                del self.vaults[identity]
                self.credentials.pop(identity, None)
                return httpx.Response(200, json={"id": identity, "deleted": True})
            return httpx.Response(200, json=self.vaults[identity])
        if path == "/v1/agents/sessions/s":
            if request.method == "DELETE":
                self.sessions.pop("s", None)
                return httpx.Response(200, json={"id": "s", "deleted": True})
            if "s" not in self.sessions:
                return httpx.Response(404, json={"error": {"message": "missing"}})
            return httpx.Response(200, json=self.sessions["s"])
        if request.url.path == "/v1/agents":
            body = json.loads(request.content)
            self.agent = {"id": "a", "created_at": 0, **body}
            if self.echo_defaults:
                tools = objects(self.agent["tools"])
                tools[0].update(
                    mode="live", context_size="medium", allowed_domains=None, location=None
                )
                tools[-1].update(
                    credential_id=None, request_metadata={}, connection_origin="service"
                )
                tools[-1]["transport"] = {**object_json(tools[-1]["transport"]), "headers": {}}
                self.agent["tools"] = list(tools)
            return httpx.Response(200, json=self.agent)
        if request.url.path == "/v1/agents/a":
            raw = deepcopy(self.agent)
            tools = objects(raw["tools"])
            mcp = tools[-1]
            transport = object_json(mcp["transport"])
            if self.fail == "changed_url":
                transport["server_url"] = "https://other.example.com/mcp"
            if self.fail == "changed_path":
                transport["server_url"] = URL + "/fictional-private-path"
            if self.fail == "changed_policy":
                mcp["allowed_tools"] = ["mutating_tool"]
            if self.fail == "native_auth":
                transport["authorization"] = "Bearer fictional-native-secret"
            if self.fail == "native_headers":
                transport["headers"] = {"Authorization": "Bearer fictional-native-secret"}
            if self.fail == "native_credential":
                mcp["credential_id"] = "foreign-credential"
            if self.fail == "environment_origin":
                mcp["connection_origin"] = "environment"
            mcp["transport"] = transport
            raw["tools"] = list(tools)
            if self.fail == "duplicate_server":
                raw["tools"] = [*tools, mcp]
            if self.fail == "foreign_record":
                raw["metadata"] = {**object_json(raw["metadata"]), "mux_tenant": "other-tenant"}
            return httpx.Response(200, json=raw)
        assert request.url.path == "/v1/agents/sessions" and request.method == "POST"
        if self.fail == "session_400":
            return httpx.Response(
                400, json={"error": {"message": "invalid fixture", "code": "bad_fixture"}}
            )
        if self.fail == "session_network":
            raise httpx.ReadTimeout("offline network uncertainty", request=request)
        response = native_session()
        response["vault_ids"] = json.loads(request.content).get("vault_ids", [])
        override = object_json(object_json(json.loads(request.content)).get("agent") or {})
        returned_agent = object_json(response["agent"])
        for field in ("model", "multi_agent"):
            if field in override:
                returned_agent[field] = override[field]
        response["agent"] = returned_agent
        self.sessions["s"] = response
        return httpx.Response(200, json=response)

    def driver(
        self, sdk: AsyncOpenAI, *, resolver: bool = True, controls: SessionControls | None = None
    ) -> OpenAIDriver:
        return OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            mcp_secrets=self.resolve if resolver else None,
            session_controls=controls,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("echo_defaults", [False, True])
async def test_bearer_is_session_only_and_sdk_debug_logging_redacts_it(
    caplog: pytest.LogCaptureFixture,
    echo_defaults: bool,
) -> None:
    wire = Wire()
    wire.echo_defaults = echo_defaults
    caplog.set_level(logging.DEBUG, logger="openai")
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        restored = await driver.agents.retrieve(SCOPE, agent.ref)
        assert restored.spec.mcp_servers == agent.spec.mcp_servers == spec().mcp_servers
        assert not wire.resolve_calls
        await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="session",
        )
    saved = json.loads(wire.requests[0].content)
    assert wire.secret not in wire.requests[0].content.decode()
    assert "authorization" not in saved["tools"][-1]["transport"]
    assert KEY in saved["metadata"] and "host:owned-token" in saved["metadata"][KEY]
    session = json.loads(wire.requests[-1].content)
    assert session["agent_id"] == "a"
    assert session["agent"]["tools"][0] == {"type": "web_search"}
    mcp = session["agent"]["tools"][-1]
    assert mcp == {
        "type": "mcp",
        "server_label": "daimon-mcp",
        "transport": {"type": "http", "server_url": URL},
        "credential_id": "credential-vault-1",
        "connection_origin": "service",
        "allowed_tools": ["describe_agent", "list_my_sessions"],
        "required": True,
    }
    assert session["vault_ids"] == ["vault-1"]
    assert wire.credentials["vault-1"]["auth"]["token"] == wire.secret
    assert wire.secret not in json.dumps(session)
    assert wire.resolve_calls == [(SCOPE, "host:owned-token", URL)]
    assert wire.requests[-1].headers["Idempotency-Key"] == "session"
    assert wire.secret not in caplog.text and "fictional-native-secret" not in caplog.text
    assert wire.secret not in repr(agent) and wire.secret not in repr(restored)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "secret_failure",
        "changed_url",
        "changed_path",
        "changed_policy",
        "native_auth",
        "native_headers",
        "native_credential",
        "environment_origin",
        "duplicate_server",
        "no_resolver",
        "foreign_scope",
        "foreign_tenant",
        "foreign_record",
    ],
)
@pytest.mark.parametrize("echo_defaults", [False, True])
async def test_failed_or_foreign_binding_refuses_before_session_write(
    failure: str, echo_defaults: bool
) -> None:
    wire = Wire()
    wire.echo_defaults = echo_defaults
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk, resolver=failure != "no_resolver")
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        wire.fail = failure
        scope = SCOPE
        if failure == "foreign_scope":
            scope = SCOPE.model_copy(update={"account_id": "other"})
        if failure == "foreign_tenant":
            scope = SCOPE.model_copy(update={"tenant_id": "other-tenant"})
        with pytest.raises((ProviderError, UnsupportedCapability, ScopeViolation)):
            await driver.sessions.create(
                scope,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="refuse",
            )
    assert all(r.url.path != "/v1/agents/sessions" for r in wire.requests)
    assert len(wire.resolve_calls) == (1 if failure == "secret_failure" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection",
    [
        MCPConnection(name="m", url=url, credential_ref=reference)
        for reference in (None, "host:owned-token")
        for url in (
            "http://qa.example.com/mcp",
            "https://user:token@qa.example.com/mcp",
            "https://qa.example.com/mcp?token=secret",
            "https://qa.example.com/mcp#secret",
            "https://@qa.example.com/mcp",
            "https://qa.example.com/mcp?",
            "https://qa.example.com/mcp#",
            "https://qa.example.com/mcp\n",
            "https://qa.example.com:invalid/mcp",
            "https://user%3Asecret%40qa.example.com/mcp",
            "https://qa.example.com\\@secondary.example.com/mcp",
        )
    ]
    + [
        MCPConnection(name="m", url=URL, credential_ref="Bearer plaintext-secret"),
        MCPConnection(
            name="m",
            url=URL,
            credential_ref="host:owned-token",
            tool_policy={"headers": {"Authorization": "secret"}},
        ),
        MCPConnection(name="m", url=URL, tool_policy={"allowed_tools": ["one", "one"]}),
        MCPConnection(name="m", url=URL, tool_policy={"required": 1}),
    ],
)
async def test_invalid_policy_or_destination_refuses_before_any_io(
    connection: MCPConnection,
) -> None:
    wire = Wire()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        with pytest.raises(UnsupportedCapability):
            await wire.driver(sdk).agents.create(SCOPE, spec(connection), key="refuse")
    assert not wire.requests and not wire.resolve_calls


@pytest.mark.asyncio
async def test_reserved_metadata_cannot_inject_host_credential_references() -> None:
    wire = Wire()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        with pytest.raises(UnsupportedCapability):
            await wire.driver(sdk).agents.create(
                SCOPE, spec().model_copy(update={"metadata": {KEY: "forged"}}), key="refuse"
            )
    assert not wire.requests and not wire.resolve_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [{"allowed_tools": []}, {"required": False}, {}])
async def test_anonymous_policy_and_omissions_remain_explicit(policy: dict[str, JsonValue]) -> None:
    wire = Wire()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk, resolver=False)
        connection = MCPConnection(name="daimon-mcp", url=URL, tool_policy=policy)
        agent = await driver.agents.create(SCOPE, spec(connection), key="public")
        await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="public-session",
        )
    saved = json.loads(wire.requests[0].content)
    assert saved["tools"][-1] == {
        "type": "mcp",
        "server_label": "daimon-mcp",
        "transport": {"type": "http", "server_url": URL},
        **policy,
    }
    assert KEY not in saved["metadata"]
    assert "agent" not in json.loads(wire.requests[-1].content)
    assert not wire.resolve_calls
    if not policy:
        # Captured through the actual SDK and unchanged agent/session modules
        # at integration d787bc0be (#689), before the auth slice. Key order is
        # part of the request proof, independent of parsed dict equality.
        assert wire.requests[0].content == (
            b'{"name":"fixture","model":"gpt-6-luna","tools":[{"type":"web_search"},'
            b'{"type":"mcp","server_label":"daimon-mcp","transport":{"type":"http",'
            b'"server_url":"https://qa.example.com/mcp"}}],"metadata":{"mux_tenant":"t"}}'
        )
        assert wire.requests[-1].content == (
            b'{"agent_id":"a","environment":{"type":"openai_hosted"},'
            b'"metadata":{"mux_tenant":"t","mux_config_revision":"1"}}'
        )


@pytest.mark.asyncio
async def test_metadata_capacity_failure_never_creates_or_resolves():
    wire = Wire()
    oversized = spec().model_copy(
        update={"metadata": {"slot" + str(i): "value" for i in range(15)}}
    )
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        with pytest.raises(UnsupportedCapability):
            await wire.driver(sdk).agents.create(SCOPE, oversized, key="full")
    assert not wire.requests and not wire.resolve_calls


def test_eleven_long_skill_pins_and_mcp_intent_both_survive_agent_round_trip() -> None:
    from .conftest import FakeTransport

    context = Context(FakeTransport(), "project", "openai.persistent_workspace", None)
    pins = tuple(
        SkillRef(id="skill_" + "x" * 42 + str(n), version="1", digest="a" * 64) for n in range(11)
    )
    requested = spec().model_copy(update={"skills": pins})
    body = agent_body(requested, context)
    assert decode(body["metadata"]) == pins
    assert KEY in object_json(body["metadata"])
    tools = objects(body["tools"])
    tools[0].update(mode="live", context_size="medium", allowed_domains=None, location=None)
    tools[-1].update(credential_id=None, request_metadata={}, connection_origin="service")
    tools[-1]["transport"] = {**object_json(tools[-1]["transport"]), "headers": {}}
    body["tools"] = list(tools)
    restored = agent_spec(body, context)
    assert restored.tools == (ToolSpec(name="web_search", kind="builtin"),)
    assert restored.skills == pins and restored.mcp_servers == requested.mcp_servers
    assert len(object_json(body["metadata"])) <= 15


def test_function_tool_decode_tolerates_the_provider_added_defer_loading_flag() -> None:
    """Observed live against the real OpenAI API: every function tool on a
    created agent echoes `defer_loading: false`, even though this driver
    never sets it — see `_non_mcp_tool` in mcp_auth.py."""
    from .conftest import FakeTransport

    context = Context(FakeTransport(), "project", "openai.persistent_workspace", None)
    body: Object = {
        "name": "fixture",
        "model": "gpt-6-luna",
        "tools": [
            {
                "type": "function",
                "name": "diag_tool",
                "description": "diag",
                "parameters": {"type": "object", "properties": {}},
                "defer_loading": False,
            }
        ],
    }
    restored = agent_spec(body, context)
    assert restored.tools == (
        ToolSpec(
            name="diag_tool",
            kind="custom",
            description="diag",
            input_schema={"type": "object", "properties": {}},
        ),
    )


def test_function_tool_decode_refuses_a_non_boolean_defer_loading_flag() -> None:
    from .conftest import FakeTransport

    context = Context(FakeTransport(), "project", "openai.persistent_workspace", None)
    body: Object = {
        "name": "fixture",
        "model": "gpt-6-luna",
        "tools": [
            {
                "type": "function",
                "name": "diag_tool",
                "parameters": {"type": "object", "properties": {}},
                "defer_loading": "soon",
            }
        ],
    }
    with pytest.raises(ValueError, match="defer_loading"):
        agent_spec(body, context)


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["", "token\rvalue", "token\nvalue", None, 123])
async def test_invalid_resolved_secret_never_reaches_wire_or_error(secret: object) -> None:
    wire = Wire()

    async def invalid(scope: Scope, ref: str, destination: str):
        return secret

    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        refusing = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            mcp_secrets=cast(MCPSecretResolver, invalid),
        )
        with pytest.raises(ProviderError) as caught:
            await refusing.sessions.create(
                SCOPE,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="refuse",
            )
    assert caught.value.category == "permission"
    assert caught.value.native_code == "mcp_secret_unavailable"
    assert caught.value.__cause__ is None
    assert all(r.url.path != "/v1/agents/sessions" for r in wire.requests)


@pytest.mark.asyncio
async def test_inline_mcp_and_vault_auth_refuse_before_resolving_or_writing() -> None:
    wire = Wire()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        vault = agent.ref.model_copy(update={"kind": "vault", "id": "owned-vault"})
        with pytest.raises(UnsupportedCapability):
            await driver.sessions.create(
                SCOPE,
                SessionSpec(
                    agent=agent.ref,
                    agent_revision=Revision(local=0),
                    config_revision=1,
                    extensions={
                        "openai.vaults": ExtensionConfig(
                            namespace="openai.vaults",
                            version=1,
                            value={"vaults": [vault.model_dump(mode="json")]},
                        )
                    },
                ),
                key="mixed-auth",
            )
    assert not wire.resolve_calls
    assert all(r.url.path != "/v1/agents/sessions" for r in wire.requests)


@pytest.mark.asyncio
async def test_concurrent_accounts_resolve_fresh_scoped_bearers_without_cross_contamination() -> (
    None
):
    wire = Wire()
    other = SCOPE.model_copy(update={"account_id": "other-account", "principal_id": "other-user"})
    resolved: list[tuple[Scope, str, str]] = []

    async def per_account(scope: Scope, ref: str, destination: str) -> str:
        assert scope in (SCOPE, other) and ref == "host:owned-token" and destination == URL
        resolved.append((scope, ref, destination))
        await asyncio.sleep(0)
        return "fictional-" + scope.account_id

    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope in (SCOPE, other),
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            mcp_secrets=per_account,
        )
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        await asyncio.gather(
            *(
                driver.sessions.create(
                    scope,
                    SessionSpec(
                        agent=agent.ref.model_copy(update={"account_id": scope.account_id}),
                        agent_revision=Revision(local=0),
                        config_revision=1,
                    ),
                    key="session-" + scope.account_id,
                )
                for scope in (SCOPE, other)
            )
        )
    sessions = [r for r in wire.requests if r.url.path == "/v1/agents/sessions"]
    assert len(sessions) == len(resolved) == 2
    for request in sessions:
        account = request.headers["Idempotency-Key"].removeprefix("session-")
        body = json.loads(request.content)
        vault = body["vault_ids"][0]
        assert wire.credentials[vault]["auth"]["token"] == "fictional-" + account
        assert "authorization" not in body["agent"]["tools"][-1]["transport"]
    assert "authorization" not in object_json(objects(wire.agent["tools"])[-1]["transport"])


async def test_auth_preserves_the_admitted_g1_model_and_delegation_controls() -> None:
    # This requires G1's real control edge; no fabricated test-only settings.
    controls = SessionControls.model_validate({"model": "gpt-6-luna", "multi_agent_enabled": False})
    wire = Wire()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk, controls=controls)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="g1-auth-session",
        )
    override = object_json(object_json(json.loads(wire.requests[-1].content))["agent"])
    assert override["model"] == "gpt-6-luna"
    assert override["multi_agent"] == {"enabled": False}
    tools = objects(override["tools"])
    assert tools[0] == {"type": "web_search"}
    assert "authorization" not in object_json(tools[-1]["transport"])
    assert wire.credentials["vault-1"]["auth"]["token"] == wire.secret


async def test_authenticated_override_refuses_anonymous_unbound_destination() -> None:
    wire = Wire()
    requested = spec().model_copy(
        update={
            "mcp_servers": (
                MCPConnection(name="public", url="https://public.example.com/mcp"),
                *(spec().mcp_servers or ()),
            )
        }
    )
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        with pytest.raises(UnsupportedCapability) as refused:
            await wire.driver(sdk).agents.create(SCOPE, requested, key="mixed-tools-agent")
    assert refused.value.missing == ("unbound_mcp_destination",)
    assert not wire.requests and not wire.resolve_calls


async def test_cancelled_resolver_cancels_before_session_post() -> None:
    wire = Wire()

    async def cancelled(scope: Scope, ref: str, destination: str) -> str:
        raise asyncio.CancelledError()

    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, _kind, _id: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            mcp_secrets=cancelled,
        )
        agent = await driver.agents.create(SCOPE, spec(), key="cancel-agent")
        with pytest.raises(asyncio.CancelledError):
            await driver.sessions.create(
                SCOPE,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="cancel-session",
            )
    assert all(r.url.path != "/v1/agents/sessions" for r in wire.requests)


@pytest.mark.parametrize("has_bound_server", [True, False])
@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize(
    "authentication",
    [
        "authorization",
        "headers",
        "credential_id",
        "credential_ref",
        "transport_credential",
        "environment_origin",
        "url_userinfo",
        "url_empty_userinfo",
        "url_query",
        "url_fragment",
        "url_http",
        "root_headers",
        "root_auth",
        "transport_auth",
        "transport_token",
        "root_token",
        "MCP",
        "Mcp",
        "mcp ",
        " mcp",
        "mcp\n",
        "remote_mcp",
        "unknown",
        "FUNCTION",
        "function_headers",
        "function_authorization",
        "function_server_url",
        "function_credential_ref",
        "function_api_key",
        "function_auth",
        "function_transport",
        "web_search_authorization",
        "tool_search_headers",
        "programmatic_tool_calling_auth",
    ],
)
async def test_unbound_secondary_auth_refuses_before_any_resolver_or_session_post(
    has_bound_server: bool,
    position: str,
    authentication: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wire = Wire()
    sentinel = "fictional-unbound-secondary-private-value"
    caplog.set_level(logging.DEBUG)

    def malicious_native_agent(request: httpx.Request) -> httpx.Response:
        response = wire.handle(request)
        if request.url.path != "/v1/agents/a":
            return response
        raw = object_json(response.json())
        secondary: Object = {
            "type": "mcp",
            "server_label": "secondary",
            "transport": {"type": "http", "server_url": "https://secondary.example.com/mcp"},
        }
        transport = object_json(secondary["transport"])
        if authentication.startswith("url_"):
            transport["server_url"] = {
                "url_userinfo": "https://user:" + sentinel + "@secondary.example.com/mcp",
                "url_empty_userinfo": "https://@secondary.example.com/mcp",
                "url_query": "https://secondary.example.com/mcp?token=" + sentinel,
                "url_fragment": "https://secondary.example.com/mcp#" + sentinel,
                "url_http": "http://secondary.example.com/mcp",
                "url_path": "https://secondary.example.com/" + sentinel,
            }[authentication]
        elif authentication == "root_headers":
            secondary["headers"] = {"X-Api-Key": sentinel}
        elif authentication == "root_auth":
            secondary["auth"] = {"password": sentinel}
        elif authentication == "transport_auth":
            transport["auth"] = {"password": sentinel}
        elif authentication == "transport_token":
            transport["access_token"] = sentinel
        elif authentication == "root_token":
            secondary["api_key"] = sentinel
        elif authentication == "authorization":
            transport["authorization"] = "Bearer " + sentinel
        elif authentication == "headers":
            transport["headers"] = {"Authorization": "Bearer " + sentinel}
        elif authentication == "transport_credential":
            transport["credential_id"] = sentinel
        elif authentication == "environment_origin":
            secondary["connection_origin"] = "environment"
            secondary["credential_id"] = sentinel
        else:
            secondary[authentication] = sentinel
        secondary["transport"] = transport
        if authentication in (
            "MCP",
            "Mcp",
            "mcp ",
            " mcp",
            "mcp\n",
            "remote_mcp",
            "unknown",
            "FUNCTION",
        ):
            secondary["type"] = authentication
            transport["authorization"] = "Bearer " + sentinel
            if authentication == "Mcp":
                transport["server_url"] = "https://user:" + sentinel + "@secondary.example.com/mcp"
            if authentication == "remote_mcp":
                secondary["server_url"] = URL
                secondary["authorization"] = sentinel
            secondary["transport"] = transport
        elif authentication.startswith("function_"):
            secondary = {
                "type": "function",
                "name": "unsafe",
                "description": "Unsafe",
                "parameters": {},
            }
            field = authentication.removeprefix("function_")
            secondary[field] = {"Authorization": sentinel} if field == "headers" else sentinel
        elif authentication in (
            "web_search_authorization",
            "tool_search_headers",
            "programmatic_tool_calling_auth",
        ):
            kind, field = authentication.rsplit("_", 1)
            secondary = {"type": kind, field: sentinel}
        native = list(objects(raw["tools"]))
        raw["tools"] = [secondary, *native] if position == "first" else [*native, secondary]
        return httpx.Response(200, json=raw)

    async with build_async_openai(
        transport=httpx.MockTransport(malicious_native_agent),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        requested = spec() if has_bound_server else spec(MCPConnection(name="public", url=URL))
        agent = await driver.agents.create(SCOPE, requested, key="agent")
        with pytest.raises(ProviderError) as refused:
            await driver.sessions.create(
                SCOPE,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="unbound-session",
            )
    assert refused.value.native_code == "malformed_response"
    assert refused.value.__cause__ is None
    assert all(request.url.path != "/v1/agents/sessions" for request in wire.requests)
    assert wire.resolve_calls == []
    assert sentinel not in caplog.text and wire.secret not in caplog.text
    assert sentinel not in repr(refused.value) and sentinel not in repr(agent)
    # The assertion must exercise the SDK's real DEBUG options logging.
    assert any(
        record.name.startswith("openai") and record.levelno == logging.DEBUG
        for record in caplog.records
    )


async def test_clean_secondary_destination_requires_exact_scoped_binding() -> None:
    wire = Wire()

    def unbound_destination(request: httpx.Request) -> httpx.Response:
        response = wire.handle(request)
        if request.url.path != "/v1/agents/a":
            return response
        raw = object_json(response.json())
        raw["tools"] = [
            *objects(raw["tools"]),
            {
                "type": "mcp",
                "server_label": "secondary",
                "transport": {"type": "http", "server_url": "https://secondary.example.com/mcp"},
            },
        ]
        return httpx.Response(200, json=raw)

    async with build_async_openai(
        transport=httpx.MockTransport(unbound_destination),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        with pytest.raises(ProviderError):
            await driver.sessions.create(
                SCOPE,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="unbound-session",
            )
    assert wire.resolve_calls == []
    assert all(request.url.path != "/v1/agents/sessions" for request in wire.requests)


@pytest.mark.parametrize("strict", [None, False, True])
async def test_single_bound_mcp_and_allowlisted_tools_preserve_bytes_and_redact_debug(
    strict: bool | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wire = Wire()
    caplog.set_level(logging.DEBUG)
    resolved: list[tuple[Scope, str, str]] = []
    values = {
        ("host:owned-token", URL): "fictional-first-bound-bearer",
    }

    async def resolve(scope: Scope, ref: str, destination: str) -> str:
        assert scope == SCOPE
        resolved.append((scope, ref, destination))
        return values[(ref, destination)]

    def handle(request: httpx.Request) -> httpx.Response:
        response = wire.handle(request)
        if request.url.path != "/v1/agents/a" or strict is None:
            return response
        raw = object_json(response.json())
        tools = objects(raw["tools"])
        for tool in tools:
            if tool["type"] == "function":
                tool["strict"] = strict
        wire.agent["tools"] = raw["tools"] = list(tools)
        return httpx.Response(200, json=raw)

    requested = spec().model_copy(
        update={
            "tools": (
                *(spec().tools or ()),
                ToolSpec(name="tool_search", kind="builtin"),
                ToolSpec(name="programmatic_tool_calling", kind="builtin"),
                ToolSpec(
                    name="owned_helper",
                    kind="custom",
                    description="",
                    input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                ),
            ),
        }
    )
    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, _kind, _id: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            mcp_secrets=resolve,
        )
        agent = await driver.agents.create(SCOPE, requested, key="agent")
        await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="single-bound-session",
        )
    assert resolved == [(SCOPE, ref, url) for ref, url in values]
    posts = [request for request in wire.requests if request.url.path == "/v1/agents/sessions"]
    assert len(posts) == 1
    tools = objects(object_json(object_json(json.loads(posts[0].content))["agent"])["tools"])
    saved = objects(wire.agent["tools"])
    assert len(saved) == len(tools) == 5
    for original, overridden in zip(saved[:4], tools[:4], strict=True):
        assert json.dumps(original, separators=(",", ":")) == json.dumps(
            overridden, separators=(",", ":")
        )
    assert all(
        "authorization" not in object_json(tool["transport"])
        for tool in tools
        if tool["type"] == "mcp"
    )
    assert [
        object_json(credential["auth"])["token"] for credential in wire.credentials.values()
    ] == list(values.values())
    assert all(value not in caplog.text and value not in repr(agent) for value in values.values())
    assert all(value not in json.dumps(wire.agent) for value in values.values())
    assert any(
        "Request options:" in record.getMessage() and "[redacted]" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        record.name.startswith("openai") and record.levelno == logging.DEBUG
        for record in caplog.records
    )


@pytest.mark.parametrize("saved", [False, True])
async def test_multiple_bound_servers_refuse_before_resolver_or_write(saved: bool) -> None:
    wire = Wire()
    connections = (
        *(spec().mcp_servers or ()),
        MCPConnection(
            name="second", url="https://second.example.com/mcp", credential_ref="host:second"
        ),
    )

    def handle(request: httpx.Request) -> httpx.Response:
        response = wire.handle(request)
        if request.url.path != "/v1/agents/a":
            return response
        raw = object_json(response.json())
        raw["metadata"] = {
            **object_json(raw["metadata"]),
            KEY: json.dumps(
                [c.model_dump(mode="json") for c in connections], separators=(",", ":")
            ),
        }
        raw["tools"] = [
            *objects(raw["tools"]),
            {
                "type": "mcp",
                "server_label": "second",
                "transport": {"type": "http", "server_url": "https://second.example.com/mcp"},
            },
        ]
        return httpx.Response(200, json=raw)

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        if saved:
            agent = await driver.agents.create(SCOPE, spec(), key="agent")
            with pytest.raises(ProviderError) as refused:
                await driver.sessions.create(
                    SCOPE,
                    SessionSpec(
                        agent=agent.ref, agent_revision=Revision(local=0), config_revision=1
                    ),
                    key="multiple-bound-session",
                )
            assert refused.value.native_code == "malformed_response"
            assert all(r.method != "POST" for r in wire.requests[1:])
        else:
            with pytest.raises(UnsupportedCapability) as unsupported:
                await driver.agents.create(
                    SCOPE, spec().model_copy(update={"mcp_servers": connections}), key="agent"
                )
            assert unsupported.value.missing == ("single_bound_mcp_server",)
            assert wire.requests == []
    assert wire.resolve_calls == []


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("search", "mode", "cached"),
        ("search", "context_size", "high"),
        ("search", "allowed_domains", ["example.com"]),
        ("search", "location", {"country": "US"}),
        ("search", "unknown", None),
        ("mcp", "credential_id", "foreign"),
        ("mcp", "credential_id", ""),
        ("mcp", "request_metadata", {"Authorization": "fictional-secret"}),
        ("mcp", "request_metadata", None),
        ("mcp", "unknown", None),
        ("transport", "headers", {"Authorization": "fictional-secret"}),
        ("transport", "headers", None),
        ("transport", "authorization", "fictional-secret"),
    ],
)
def test_live_defaults_do_not_admit_changed_controls_or_auth_carriers(
    section: str, field: str, value: JsonValue
) -> None:
    from .conftest import FakeTransport

    context = Context(FakeTransport(), "project", "openai.persistent_workspace", None)
    body = agent_body(spec(), context)
    tools = objects(body["tools"])
    tools[0].update(mode="live", context_size="medium", allowed_domains=None, location=None)
    tools[-1].update(credential_id=None, request_metadata={}, connection_origin="service")
    transport = object_json(tools[-1]["transport"])
    transport["headers"] = {}
    changed = tools[0] if section == "search" else tools[-1] if section == "mcp" else transport
    changed[field] = value
    tools[-1]["transport"] = transport
    body["tools"] = list(tools)
    with pytest.raises(ValueError):
        agent_spec(body, context)


async def test_owned_mcp_vault_is_deleted_with_session_after_driver_reconstruction() -> None:
    wire = Wire()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        first = wire.driver(sdk)
        agent = await first.agents.create(SCOPE, spec(), key="agent")
        session = await first.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="session",
        )
        binding = session.binding
        assert binding.native_refs["mcp_vault"] == "vault-1"
        assert wire.secret not in repr(binding)
        restored = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, identity: scope == SCOPE and kind == "session",
            binding_lookup=lambda scope, identity: (
                binding if scope == SCOPE and identity == session.ref.id else None
            ),
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
        receipt = await restored.sessions.delete(SCOPE, session.ref, key="delete")
        assert {r.kind for r in receipt.deleted} == {"session", "vault"}
        assert (
            not receipt.retained and not wire.vaults and not wire.credentials and not wire.sessions
        )
        # Repeat the cleanup from the persisted binding after top-level GET404.
        await restored.sessions.delete(SCOPE, session.ref, key="delete")


@pytest.mark.parametrize("failure, cleaned", [("session_400", True), ("session_network", False)])
async def test_mcp_vault_rejection_cleanup_does_not_revoke_an_uncertain_session(
    failure: str, cleaned: bool
) -> None:
    wire = Wire()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        wire.fail = failure
        with pytest.raises(ProviderError) as refused:
            await driver.sessions.create(
                SCOPE,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="session",
            )
    if not cleaned:
        assert any("manual provider cleanup" in note for note in refused.value.__notes__)
    assert bool(wire.vaults) is not cleaned
    assert bool(wire.credentials) is not cleaned
    assert (
        sum(r.method == "POST" and r.url.path == "/v1/agents/sessions" for r in wire.requests) == 1
    )


async def test_raw_session_vault_ids_cannot_authorize_foreign_vault_cleanup() -> None:
    wire = Wire()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        session = await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="session",
        )
        wire.sessions["s"]["vault_ids"] = ["foreign-vault"]
        from mux.errors import ContinuityLost

        with pytest.raises(ContinuityLost):
            await driver.sessions.delete(SCOPE, session.ref, key="delete")
    assert not any(r.method == "DELETE" for r in wire.requests)


@pytest.mark.parametrize("secret", ["opaque-user-token", "eyJpartial.jwt-fragment"])
@pytest.mark.parametrize("phase", ["credential", "session"])
async def test_mcp_errors_suppress_cross_request_credentials(secret: str, phase: str) -> None:
    class EchoWire(Wire):
        def handle(self, request: httpx.Request) -> httpx.Response:
            failure = (
                request.url.path.endswith("/credentials")
                if phase == "credential"
                else request.url.path == "/v1/agents/sessions"
            )
            if failure and request.method == "POST":
                self.requests.append(request)
                return httpx.Response(
                    403, json={"error": {"message": secret, "code": secret, "param": secret}}
                )
            return super().handle(request)

    wire = EchoWire()
    wire.secret = secret
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        with pytest.raises(ProviderError) as refused:
            await driver.sessions.create(
                SCOPE,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="session",
            )
    error = refused.value
    assert error.category == "permission" and error.native_code == "403"
    assert error.native_message is error.native_error_code is error.native_param is None
    assert not wire.vaults
    assert not any(r.method == "GET" and r.url.path == "/v1/vaults" for r in wire.requests)
    for request in wire.requests:
        if request.url.path == "/v1/agents/sessions":
            assert secret not in request.content.decode()


@pytest.mark.parametrize("phase", ["credential", "session"])
async def test_mcp_cleanup_failure_preserves_original_error(phase: str) -> None:
    class CleanupFails(Wire):
        def handle(self, request: httpx.Request) -> httpx.Response:
            failure = (
                request.url.path.endswith("/credentials")
                if phase == "credential"
                else request.url.path == "/v1/agents/sessions"
            )
            if failure and request.method == "POST":
                return httpx.Response(400, json={"error": {"message": self.secret}})
            if request.method == "DELETE" and request.url.path.startswith("/v1/vaults/"):
                return httpx.Response(503, json={"error": {"message": self.secret}})
            return super().handle(request)

    wire = CleanupFails()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        with pytest.raises(ProviderError) as refused:
            await driver.sessions.create(
                SCOPE,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="session",
            )
    error = refused.value
    assert error.category == "invalid_request" and error.native_code == "400"
    assert error.native_message is error.native_error_code is error.native_param is None
    assert any(
        "cleanup failed (ProviderError)" in note and "vault-1" in note for note in error.__notes__
    )
    assert wire.secret not in str(error.__notes__)
    assert wire.vaults


async def test_malformed_session_ack_reports_manual_vault_cleanup() -> None:
    class Malformed(Wire):
        def handle(self, request: httpx.Request) -> httpx.Response:
            response = super().handle(request)
            if request.method == "POST" and request.url.path == "/v1/agents/sessions":
                return httpx.Response(200, json={"id": "s"})
            return response

    wire = Malformed()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        with pytest.raises(ProviderError) as refused:
            await driver.sessions.create(
                SCOPE,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="session",
            )
    assert refused.value.native_code == "malformed_response"
    assert any(
        "vault-1" in note and "manual provider cleanup" in note for note in refused.value.__notes__
    )
    assert wire.vaults and wire.sessions


async def test_owned_mcp_session_cleanup_suppresses_credential_echo() -> None:
    class DeleteEcho(Wire):
        echo = False

        def handle(self, request: httpx.Request) -> httpx.Response:
            if self.echo and request.url.path == "/v1/agents/sessions/s":
                return httpx.Response(
                    403,
                    json={
                        "error": {
                            "message": self.secret,
                            "code": self.secret,
                            "param": self.secret,
                        }
                    },
                )
            return super().handle(request)

    wire = DeleteEcho()
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        session = await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="session",
        )
        wire.echo = True
        with pytest.raises(ProviderError) as refused:
            await driver.sessions.delete(SCOPE, session.ref, key="delete")
    assert refused.value.native_code == "403"
    assert (
        refused.value.native_message
        is refused.value.native_error_code
        is refused.value.native_param
        is None
    )


async def test_acknowledged_session_delete_keeps_vault_timeout_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mux.drivers.openai.sessions as session_module

    wire = Wire()
    session_deleted = False
    slow = True

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal session_deleted
        if session_deleted and slow and request.url.path == "/v1/vaults/vault-1":
            await asyncio.sleep(1)
        response = wire.handle(request)
        if request.method == "DELETE" and request.url.path == "/v1/agents/sessions/s":
            session_deleted = True
        return response

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, spec(), key="agent")
        session = await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="session",
        )
        monkeypatch.setattr(session_module, "_DELETE_TIMEOUT", 0.05)
        with pytest.raises(ProviderError) as refused:
            await driver.sessions.delete(SCOPE, session.ref, key="delete")
        assert refused.value.native_code == "session_delete_deadline"
        assert refused.value.retryable and session_deleted and wire.vaults
        slow = False
        monkeypatch.setattr(session_module, "_DELETE_TIMEOUT", 30)
        receipt = await driver.sessions.delete(SCOPE, session.ref, key="delete")
        assert {ref.kind for ref in receipt.deleted} == {"session", "vault"}
        assert not wire.vaults and not wire.sessions
