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
        self.secret = "fictional-per-user-bearer"

    async def resolve(self, scope: object, ref: str, destination: str) -> str:
        self.resolve_calls.append((scope, ref, destination))
        if self.fail == "secret_failure":
            raise RuntimeError("fictional-private-error")
        assert scope == SCOPE and ref == "host:owned-token" and destination == URL
        return self.secret

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/agents":
            body = json.loads(request.content)
            self.agent = {"id": "a", "created_at": 0, **body}
            return httpx.Response(200, json=self.agent)
        if request.url.path == "/v1/agents/a":
            raw = deepcopy(self.agent)
            tools = objects(raw["tools"])
            mcp = tools[-1]
            transport = object_json(mcp["transport"])
            if self.fail == "changed_url":
                transport["server_url"] = "https://other.example.com/mcp"
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
        response = native_session()
        override = object_json(object_json(json.loads(request.content)).get("agent") or {})
        returned_agent = object_json(response["agent"])
        for field in ("model", "multi_agent"):
            if field in override:
                returned_agent[field] = override[field]
        response["agent"] = returned_agent
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
async def test_bearer_is_session_only_and_sdk_debug_logging_redacts_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    wire = Wire()
    caplog.set_level(logging.DEBUG, logger="openai")
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
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
        "transport": {"type": "http", "server_url": URL, "authorization": "Bearer " + wire.secret},
        "connection_origin": "service",
        "allowed_tools": ["describe_agent", "list_my_sessions"],
        "required": True,
    }
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
async def test_failed_or_foreign_binding_refuses_before_session_write(failure: str) -> None:
    wire = Wire()
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
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
        MCPConnection(name="m", url=url, credential_ref="host:owned-token")
        for url in (
            "http://qa.example.com/mcp",
            "https://user:token@qa.example.com/mcp",
            "https://qa.example.com/mcp?token=secret",
            "https://qa.example.com/mcp#secret",
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
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
    ) as sdk:
        with pytest.raises(UnsupportedCapability):
            await wire.driver(sdk).agents.create(SCOPE, spec(connection), key="refuse")
    assert not wire.requests and not wire.resolve_calls


@pytest.mark.asyncio
async def test_reserved_metadata_cannot_inject_host_credential_references() -> None:
    wire = Wire()
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
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
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
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
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
    ) as sdk:
        with pytest.raises(UnsupportedCapability):
            await wire.driver(sdk).agents.create(SCOPE, oversized, key="full")
    assert not wire.requests and not wire.resolve_calls


def test_eleven_long_skill_pins_and_mcp_intent_both_survive_agent_round_trip() -> None:
    from .conftest import FakeTransport

    context = Context(FakeTransport(), "project", "openai.persistent_workspace", None)
    pins = tuple(SkillRef(id="skill_" + "x" * 42 + str(n), version="1") for n in range(11))
    requested = spec().model_copy(update={"skills": pins})
    body = agent_body(requested, context)
    assert decode(body["metadata"]) == pins
    assert KEY in object_json(body["metadata"])
    restored = agent_spec(body, context)
    assert restored.skills == pins and restored.mcp_servers == requested.mcp_servers
    assert len(object_json(body["metadata"])) <= 15


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["", "token\rvalue", "token\nvalue", None, 123])
async def test_invalid_resolved_secret_never_reaches_wire_or_error(secret: object) -> None:
    wire = Wire()

    async def invalid(scope: Scope, ref: str, destination: str):
        return secret

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
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
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
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

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
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
        assert (
            body["agent"]["tools"][-1]["transport"]["authorization"]
            == "Bearer fictional-" + account
        )
    assert "authorization" not in object_json(objects(wire.agent["tools"])[-1]["transport"])


async def test_auth_preserves_the_admitted_g1_model_and_delegation_controls() -> None:
    # This requires G1's real control edge; no fabricated test-only settings.
    controls = SessionControls.model_validate({"model": "gpt-6-luna", "multi_agent_enabled": False})
    wire = Wire()
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
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
    assert object_json(tools[-1]["transport"])["authorization"] == "Bearer " + wire.secret


async def test_session_auth_preserves_anonymous_mcp_and_custom_tool_bytes() -> None:
    wire = Wire()
    requested = spec().model_copy(
        update={
            "tools": (
                ToolSpec(name="web_search", kind="builtin"),
                ToolSpec(
                    name="owned_helper",
                    kind="custom",
                    description="Owned helper",
                    input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                ),
            ),
            "mcp_servers": (
                MCPConnection(
                    name="public",
                    url="https://public.example.com/mcp",
                    tool_policy={"allowed_tools": [], "required": False},
                ),
                *(spec().mcp_servers or ()),
            ),
        }
    )
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
    ) as sdk:
        driver = wire.driver(sdk)
        agent = await driver.agents.create(SCOPE, requested, key="mixed-tools-agent")
        await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="mixed-tools-session",
        )
    saved = objects(wire.agent["tools"])
    overridden = objects(
        object_json(object_json(json.loads(wire.requests[-1].content))["agent"])["tools"]
    )
    assert len(saved) == len(overridden) == 4
    for original, session_tool in zip(saved[:-1], overridden[:-1], strict=True):
        assert json.dumps(original, separators=(",", ":")) == json.dumps(
            session_tool, separators=(",", ":")
        )
    assert saved[-2]["server_label"] == "public"
    assert "authorization" not in object_json(overridden[-2]["transport"])
    assert wire.resolve_calls == [(SCOPE, "host:owned-token", URL)]


async def test_cancelled_resolver_cancels_before_session_post() -> None:
    wire = Wire()

    async def cancelled(scope: Scope, ref: str, destination: str) -> str:
        raise asyncio.CancelledError()

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
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
