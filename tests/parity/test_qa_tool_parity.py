"""Frozen QA-53 inventory and offline, explicit-delta model manifest ratchet.

Compiler evidence is not a claim that an unregistered host delivered a tool.
The server is real; only the audit sink is suppressed for read-only listing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
import yaml
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.mcp.middleware import mcp_identity
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.channel_backend import RUNNABLE_PROFILES
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    DiscordSettings,
    GeminiSettings,
    McpSettings,
    NotebookSettings,
    Settings,
    SlackSettings,
)
from daimon.core.db import build_engine, build_session_factory
from daimon.core.mux_backend import TurnBackendRequest, turn_backend
from daimon.core.mux_compat import agent_spec
from daimon.testing.ma_models import ma_agent
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from fastmcp import Client
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mux.contracts.ids import ModelRef, Provider, ResourceRef, Scope
from mux.contracts.resources import AgentSpec, MCPConnection, SkillUpload, SkillUploadFile, ToolSpec
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources.agents import agent_payload
from mux.drivers.gemini.bundles import validate_bundle
from mux.drivers.gemini.core import compile_agent
from mux.drivers.gemini.default_capability import BUILTIN_MAPPING as GEMINI_MAPPING
from mux.drivers.openai._common import Context
from mux.drivers.openai.agents import agent_body
from mux.drivers.openai.default_capability import BUILTIN_MAPPING as OPENAI_MAPPING
from mux.errors import UnsupportedCapability
from pydantic import BaseModel, ConfigDict, HttpUrl, JsonValue, PostgresDsn, SecretStr

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).with_name("tool_parity")
PROFILES = {
    "anthropic": "anthropic.managed_agents",
    "openai": "openai.persistent_workspace",
    "gemini": "gemini.inline_reuse",
}
SCOPE = Scope(tenant_id="tenant", account_id="account", principal_id="qa", authorization_id="qa53")


class Reason(StrEnum):
    HOST_TURN_UNREGISTERED = "HOST_TURN_UNREGISTERED"
    HOST_RESOURCE_ANTHROPIC = "HOST_RESOURCE_ANTHROPIC"
    AUTHENTICATED_MCP_UNSUPPORTED = "AUTHENTICATED_MCP_UNSUPPORTED"
    GEMINI_DEFAULT_BUNDLE_UNSUPPORTED = "GEMINI_DEFAULT_BUNDLE_UNSUPPORTED"
    BUILTIN_NAME_SCHEMA_DELTA = "BUILTIN_NAME_SCHEMA_DELTA"
    PRIVATE_REPOSITORY_UNSUPPORTED = "PRIVATE_REPOSITORY_UNSUPPORTED"
    MEMORY_STORE_UNSUPPORTED = "MEMORY_STORE_UNSUPPORTED"
    GEMINI_CHECKPOINT_LIMIT = "GEMINI_CHECKPOINT_LIMIT"


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Exposure(Closed):
    status: Literal["OK", "GAP", "PENDING-G1", "PENDING-G2"]
    reasons: tuple[Reason, ...]


class Scenario(Closed):
    id: str
    title: str
    surface: str
    source_sha256: str
    path: Literal["host", "turn", "resource"]
    skills_used: tuple[str, ...]
    tools: tuple[str, ...]
    resources: tuple[str, ...]
    exposure: dict[str, Exposure]
    driver_gaps: dict[str, tuple[Reason, ...]]


class Inventory(Closed):
    integration_base: str
    catalog_source: str
    target_sha256: str
    default_skills: tuple[str, ...]
    reason_codes: dict[Reason, str]
    scenarios: tuple[Scenario, ...]


class BuiltinDelta(Closed):
    reason: Reason
    missing: frozenset[str]
    added: frozenset[str]
    native_schema: Literal["provider-owned-unavailable"]


def authored() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())


def inventory() -> Inventory:
    return Inventory.model_validate_json((DATA / "map.json").read_text())


def encode(provider: str, tools: tuple[ToolSpec, ...], mcp: MCPConnection | None = None):
    spec = AgentSpec(
        name="daimon",
        model=ModelRef(provider=cast(Provider, provider), id="offline-model"),
        tools=tools,
        mcp_servers=() if mcp is None else (mcp,),
    )
    if provider == "anthropic":
        return agent_payload(spec)
    if provider == "gemini":
        return compile_agent(spec)
    # Pure encoder: no transport method can be called here.
    return agent_body(spec, Context(cast(Any, None), "offline", PROFILES[provider], None))


def custom_manifest(provider: str, tools: tuple[ToolSpec, ...]) -> dict[str, JsonValue]:
    body = encode(provider, tools)
    result: dict[str, JsonValue] = {}
    for tool in cast(list[dict[str, JsonValue]], body["tools"]):
        assert tool["type"] == ("custom" if provider == "anthropic" else "function")
        name = cast(str, tool["name"])
        assert name not in result, "duplicate model-facing name"
        result[name] = tool["input_schema" if provider == "anthropic" else "parameters"]
    return result


def test_frozen_53_map_is_complete_and_typed() -> None:
    data = inventory()
    target = (DATA / "TARGET-53.txt").read_bytes()
    assert hashlib.sha256(target).hexdigest() == data.target_sha256
    assert len(data.scenarios) == len(set(target.decode().splitlines())) == 53
    assert [s.id for s in data.scenarios] == target.decode().splitlines()
    assert set(data.reason_codes) == set(Reason)
    assert data.default_skills == tuple(s["skill_id"] for s in authored()["skills"])
    for scenario in data.scenarios:
        assert set(scenario.exposure) == set(scenario.driver_gaps) == set(PROFILES)
        assert set(scenario.skills_used) <= set(data.default_skills)
        assert len(scenario.source_sha256) == 64 and scenario.resources
        for provider, exposure in scenario.exposure.items():
            assert (exposure.status == "OK") == (not exposure.reasons)
            if exposure.status.startswith("PENDING"):
                assert exposure.status == {"openai": "PENDING-G1", "gemini": "PENDING-G2"}[provider]
                assert exposure.reasons == (Reason.HOST_TURN_UNREGISTERED,)


@pytest.mark.parametrize("provider", PROFILES)
async def test_actual_host_manifest_absence_requires_exact_pending_delta(provider: str) -> None:
    script = ScriptedTransport()
    async with script.client() as client:
        session = ResourceRef(
            id="session-offline",
            kind="session",
            provider=cast(Provider, provider),
            account_scope_id="offline",
            tenant_id=SCOPE.tenant_id,
            account_id=SCOPE.account_id,
        )
        request = TurnBackendRequest(
            profile=PROFILES[provider],
            client=client,
            scope=SCOPE,
            session_id=session.id,
            session=session,
        )
        if provider == "anthropic":
            bound = turn_backend(request)
            assert type(bound.backend) is AnthropicManagedAgents
            assert PROFILES[provider] in RUNNABLE_PROFILES
        else:
            # A merged G1/G2 registration deliberately fails this stale allowlist;
            # replace it with actual host manifest evidence before removing it.
            with pytest.raises(UnsupportedCapability) as refused:
                turn_backend(request)
            assert refused.value.missing == ("host_turn_backend",)
            assert PROFILES[provider] not in RUNNABLE_PROFILES
            assert all(
                s.exposure[provider].reasons == (Reason.HOST_TURN_UNREGISTERED,)
                for s in inventory().scenarios
                if s.path == "turn"
            )
        script.assert_consumed()
        assert script.requests == []


async def test_actual_sdk_default_anthropic_manifest_preserves_authored_tools() -> None:
    source = authored()
    # Immutable concrete native pins stand in for separately uploaded resources.
    source["skills"] = [dict(pin, version="1") for pin in source["skills"]]
    source["mcp_servers"] = [
        {"type": "url", "name": "daimon-mcp", "url": "https://offline.invalid/mcp"}
    ]
    source["tools"].append({"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"})
    script = ScriptedTransport()
    response_tools = copy.deepcopy(source["tools"])
    for tool in response_tools:
        tool["default_config"] = {"enabled": True, "permission_policy": {"type": "always_allow"}}
        tool["configs"] = [
            dict(config, enabled=True, permission_policy={"type": "always_allow"})
            for config in tool.get("configs", [])
        ]
    native = ma_agent(
        model=source["model"],
        system=source["system"],
        tools=response_tools,
        mcp_servers=source["mcp_servers"],
        skills=source["skills"],
        tenant_id=SCOPE.tenant_id,
    )
    script.queue(
        ScriptedReply(
            "POST", "/v1/agents", httpx.Response(200, json=native.model_dump(mode="json"))
        )
    )
    async with script.client() as client:
        driver = AnthropicManagedAgents(
            client, account_scope_id="offline", authorization=ResourceAuthorization(SCOPE)
        )
        await driver.agents.create(SCOPE, agent_spec(source), key="qa53-default")
    script.assert_consumed()
    body = cast(dict[str, Any], script.requests[0].json())
    assert body["tools"] == source["tools"]
    assert body["mcp_servers"] == source["mcp_servers"]
    assert body["skills"] == source["skills"]


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_default_native_names_have_only_the_typed_allowlisted_delta(provider: str) -> None:
    names = tuple(c["name"] for c in authored()["tools"][0]["configs"])
    raw_tools = tuple(ToolSpec(name=n, kind="builtin") for n in names)
    with pytest.raises(UnsupportedCapability) as refused:
        encode(provider, raw_tools)
    assert refused.value.missing == (("agent_tool",) if provider == "openai" else ("tool",))
    mapping = OPENAI_MAPPING if provider == "openai" else GEMINI_MAPPING
    assert set(mapping) == set(names)
    mapped = tuple(dict.fromkeys(tool.name for tool in mapping.values()))
    observed = encode(provider, tuple(ToolSpec(name=n, kind="builtin") for n in mapped))
    # OpenAI hosted bash is implicit in the persistent workspace environment.
    native_names = {
        cast(str, t["type"]) for t in cast(list[dict[str, JsonValue]], observed["tools"])
    }
    if provider == "openai":
        assert native_names == set()
        native_names.add("bash")
    delta = BuiltinDelta.model_validate_json((DATA / f"{provider}-builtin-delta.json").read_text())
    assert delta.reason == Reason.BUILTIN_NAME_SCHEMA_DELTA
    assert set(names) - native_names == delta.missing
    assert native_names - set(names) == delta.added


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_authenticated_default_mcp_gap_is_an_actual_driver_refusal(provider: str) -> None:
    mcp = MCPConnection(
        name="daimon-mcp", url="https://offline.invalid/mcp", credential_ref="opaque-ref"
    )
    with pytest.raises(UnsupportedCapability) as refused:
        encode(provider, (), mcp)
    assert refused.value.missing == (
        ("mcp_credentials",) if provider == "openai" else ("mcp_credentials_or_policy",)
    )
    assert any(
        Reason.AUTHENTICATED_MCP_UNSUPPORTED in s.driver_gaps[provider]
        for s in inventory().scenarios
    )


def test_full_default_gemini_bundle_gap_is_not_a_text_only_surrogate() -> None:
    failures: list[str] = []
    for name in inventory().default_skills:
        directory = ROOT / "defaults/skills" / name
        bundle = SkillUpload(
            files=tuple(
                SkillUploadFile(path=p.relative_to(directory).as_posix(), content=p.read_bytes())
                for p in sorted(directory.rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts
            )
        )
        try:
            validate_bundle(bundle)
        except (UnsupportedCapability, ValueError):
            failures.append(name)
    assert failures == ["pymc-artifact-style"]


async def mcp_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, JsonValue]]:
    # Same authenticated caller/context; never give an alternate backend a more
    # permissive admin/global registry and call that default MCP parity.
    for name in tuple(os.environ):
        if name.startswith(("DAIMON_", "STRIPE_")) or name == "MCP_PUBLIC_URL":
            monkeypatch.delenv(name)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost:1/offline")),
        anthropic=AnthropicSettings(api_key=SecretStr("offline")),
        mcp=McpSettings(
            jwt_secret=SecretStr("x" * 32), public_url=HttpUrl("https://offline.invalid/mcp")
        ),
        crypto=CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),)),
        discord=DiscordSettings(bot_token=SecretStr("offline")),
        slack=SlackSettings(signing_secret=SecretStr("offline"), app_token=SecretStr("offline")),
        gemini=GeminiSettings(api_key=SecretStr("offline")),
        notebook=NotebookSettings(
            host_url=HttpUrl("http://offline.invalid:8001"), admin_secret=SecretStr("offline")
        ),
    )
    engine = build_engine(str(settings.database.url))
    # Assert listing is truly DB-free, including audit, rather than silently
    # swallowing a failed connection to a real database.
    from sqlalchemy import event

    def no_database(*_args: object) -> None:
        raise AssertionError("offline manifest tried to connect to a database")

    event.listen(engine.sync_engine, "do_connect", no_database)

    def discard_audit(*_args: object) -> None:
        pass

    monkeypatch.setattr(mcp_identity.IdentityMiddleware, "_queue_audit", discard_audit)
    manifests: dict[str, dict[str, JsonValue]] = {}
    async with AsyncAnthropic(
        api_key="offline",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: (_ for _ in ()).throw(AssertionError("provider I/O"))
            )
        ),
    ) as anthropic:
        app = create_mcp_app(
            settings=settings,
            sessionmaker=build_session_factory(
                engine,
                crypto_keys=tuple(key.get_secret_value() for key in settings.crypto.keys),
                allow_plaintext=False,
            ),
            auth=StaticTokenVerifier(tokens={}),
            anthropic=anthropic,
        )
        for platform in ("discord", "slack", "teams"):
            for role in ("user", "admin", "agent"):
                token = AccessToken(
                    token="offline",
                    client_id="offline",
                    scopes=[],
                    claims={
                        "sub": "11111111-1111-1111-1111-111111111111",
                        "tenant_id": "22222222-2222-2222-2222-222222222222",
                        "role": "user" if role == "agent" else role,
                        "platform": platform,
                        "chat_agent_id": "33333333-3333-3333-3333-333333333333",
                        **(
                            {"agent_id": "33333333-3333-3333-3333-333333333333"}
                            if role == "agent"
                            else {}
                        ),
                    },
                )
                monkeypatch.setattr(mcp_identity, "get_access_token", lambda token=token: token)
                async with Client(app.state.mcp) as client:
                    rendered = await client.list_tools()
                    if role != "agent":
                        required = {
                            name
                            for scenario in inventory().scenarios
                            if scenario.surface == platform
                            for name in scenario.tools
                            if name
                            not in {
                                "bash",
                                "read",
                                "write",
                                "edit",
                                "grep",
                                "glob",
                                "search_tools",
                                "call_tool",
                            }
                        }
                        for name in sorted(required - {t.name for t in rendered}):
                            found = await client.call_tool("search_tools", {"query": name})
                            text = "\n".join(getattr(part, "text", "") for part in found.content)
                            assert name in text, (platform, role, name, text)
                tools = tuple(
                    ToolSpec(name=t.name, kind="custom", input_schema=t.inputSchema)
                    for t in rendered
                )
                expected: dict[str, JsonValue] = {
                    t.name: cast(JsonValue, t.inputSchema) for t in rendered
                }
                if role == "agent":
                    assert "describe_agent" in expected and "list_my_sessions" in expected
                    assert "request_agent_key" not in expected
                else:
                    assert set(expected) == {
                        "search_tools",
                        "call_tool",
                        "add_skill",
                        "publish_report",
                        "create_attachment_upload_url",
                        "create_notebook_upload_url",
                    }
                    assert "describe_agent" not in expected and "list_my_sessions" not in expected
                for provider in PROFILES:
                    assert custom_manifest(provider, tools) == expected
                manifests[f"{platform}:{role}"] = expected
    await engine.dispose()
    return manifests


async def test_same_mcp_names_and_schemas_survive_each_real_backend_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = await mcp_manifests(monkeypatch)
    # Independently pinned digest detects additions, omissions and schema drift
    # even when all three encoders drift together. Auth deltas are by caller,
    # never by provider; no admin tools are added to an alternate backend.
    pinned = json.loads((DATA / "mcp-schema-sha256.json").read_text())
    assert set(pinned) == set(manifests)
    for context, manifest in manifests.items():
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert digest == pinned[context]


@pytest.mark.parametrize("mutation", ["missing", "extra", "schema"])
def test_manifest_comparator_rejects_unallowlisted_changes(mutation: str) -> None:
    tool = ToolSpec(
        name="probe",
        kind="custom",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    )
    expected = custom_manifest("anthropic", (tool,))
    changed = (
        ()
        if mutation == "missing"
        else (tool, ToolSpec(name="extra", kind="custom"))
        if mutation == "extra"
        else (tool.model_copy(update={"input_schema": {"type": "object"}}),)
    )
    assert custom_manifest("openai", changed) != expected
