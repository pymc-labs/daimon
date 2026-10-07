from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillListResponse
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import (
    AgentInfo,
    _archive_agent_impl,
    _attach_mcp_server_impl,
    _build_create_spec,
    _create_agent_impl,
    _fork_agent_impl,
    _get_agent_impl,
    _list_agents_impl,
    _update_agent_impl,
    register_agent_tools,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_guidance import CREDENTIAL_GUIDANCE_BLOCK
from daimon.core.agent_mcp_credentials import (
    resolve_agent_mcp_credentials,
    save_agent_mcp_credential,
)
from daimon.core.constants import AGENT_MCP_CAP, AGENT_SKILL_CAP
from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.defaults.metadata import MA_METADATA_KEY_ISOLATED, tenant_scoped_display_title
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.github_credentials import build_multifernet, get_pat, upsert_credential_encrypted
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.routing_facts import UNROUTED_LINE
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.skills.ingest import bundle_from_markdown
from daimon.core.specs import AgentSpec, SkillRef, SkillRepo
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.agent_repo_binding import get_binding, set_binding
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.user_skills import upsert_user_skill
from daimon.testing import ma_agent
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMAState,
    MARouter,
    NotHandled,
    build_fake_anthropic,
    build_no_retry_anthropic,
    combine_handlers,
    json_body,
    list_response,
    make_fake_ma_handler,
)
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

# Repeated nested config required by the SDK response models. Inlined in every
# test that needs it (only the permission_policy block is shared — every other
# field is constructed at the call site per guideline:testing).
_ALLOW_ALL: dict[str, Any] = {"enabled": True, "permission_policy": {"type": "always_allow"}}


def _make_settings(*, public_url: str | None = None) -> MagicMock:
    """Return a minimal settings mock with mcp.public_url wired explicitly."""
    settings = MagicMock()
    settings.mcp.public_url = public_url
    settings.github.oauth_scopes = ("repo", "read:user")
    return settings


def _runtime(
    client: AsyncAnthropic,
    *,
    public_url: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | MagicMock | None = None,
    fernet: MultiFernet | None = None,
    deployment_default: DeploymentDefault | None = None,
) -> McpRuntime:
    return McpRuntime(
        session_factory=session_factory if session_factory is not None else MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=_make_settings(public_url=public_url),  # type: ignore[arg-type]
        deployment_default=deployment_default
        if deployment_default is not None
        else DeploymentDefault(),
        fernet=fernet,
    )


# ---------------------------------------------------------------------------
# Full-HTTP-pipeline app — FastMCP
# output-schema validation only runs through mcp.http_app() + a real JSON-RPC
# tools/call; unit-calling the _impl functions bypasses it entirely.
# ---------------------------------------------------------------------------


def _agents_mcp_app(
    client: AsyncAnthropic,
    token: str,
    claims: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> ASGIApp:
    """Assemble a minimal FastMCP app with the agents tool group registered
    behind the real auth + identity middleware pipeline."""
    mock_sessionmaker: async_sessionmaker[AsyncSession] = MagicMock()  # type: ignore[assignment]
    mcp = FastMCP(name="agents-schema-test", auth=StaticTokenVerifier(tokens={token: claims}))
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=production_subject_resolver,
            tenant_resolver=production_tenant_resolver,
            role_resolver=production_role_resolver,
            agent_id_resolver=production_agent_id_resolver,
            is_admin_resolver=production_is_admin_resolver,
            internal_resolver=production_internal_resolver,
            sessionmaker=mock_sessionmaker,
        )
    )
    register_agent_tools(mcp, _runtime(client, session_factory=session_factory))
    return mcp.http_app()


def _make_agent_payload_with_skill_type(
    *,
    tenant_id: uuid.UUID,
    agent_id: str = "ag_a",
    name: str = "demo",
    skill_type: str = "anthropic",
) -> dict[str, Any]:
    """Build a BetaManagedAgentsAgent payload via the real SDK constructor with
    a valid ``anthropic``-type skill, then override the skill's ``type`` — the
    SDK's discriminated skills union has no constructor for a novel skill
    type, so validated construction is impossible for that single field
    (mirrors test_sessions.py's ``_make_session_payload(status=...)``)."""
    payload = ma_agent(
        id=agent_id,
        name=name,
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": name},
        skills=[{"skill_id": "skill_x", "type": "anthropic", "version": "1"}],
    ).model_dump(mode="json")
    payload["skills"][0]["type"] = skill_type
    return payload


# ---------------------------------------------------------------------------
# Regression test — permissive projection through the full pipeline
# ---------------------------------------------------------------------------


async def test_get_agent_admits_novel_skill_type_through_fastmcp(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """get_agent must not output-validation-error when an attached skill's
    ``type`` is a string the pinned SDK's Literal["anthropic", "custom"] does
    not model (e.g. a new upstream skill category).

    RED on main: AgentSkillInfo.type: Literal["anthropic", "custom"] rejects
    the whole AgentInfo payload with a FastMCP output-validation error.
    """
    tenant_id = uuid.uuid4()
    token = "get-agent-novel-skill-type"
    claims = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "role": "user",
        "client_id": "test",
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [_make_agent_payload_with_skill_type(tenant_id=tenant_id, skill_type="community")]
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    app = _agents_mcp_app(client, token, claims, db_session_factory)

    result = await call_mcp_tool(app, token=token, name="get_agent", arguments={"name": "demo"})

    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert not payload.get("isError"), (
        f"get_agent must not output-validation-error on a novel skill type; got {payload!r}"
    )
    structured = payload.get("structuredContent") or {}
    skill_types = [sk.get("type") for sk in structured.get("skills", [])]  # type: ignore[union-attr]
    assert "community" in skill_types, (
        f"the novel skill type must survive in the tool output; got {skill_types!r}"
    )


async def test_list_agents_impl_returns_tenant_agents(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _list_agents_impl(
        _runtime(client, session_factory=db_session_factory), auth, page=None
    )
    assert isinstance(result, list), "should return a list"
    assert [a.name for a in result] == ["demo"], "should list the tenant's agent"


async def test_get_agent_impl_returns_agent_info(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _get_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "a")
    assert result.name == "a", "should return the requested agent"
    assert isinstance(result, AgentInfo), "should return an AgentInfo"


async def test_get_agent_impl_returns_mcp_servers_and_skills(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Issue: get_agent returned only name/id/description/model, so chat could
    never see which MCP servers or skills an agent has attached."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    name="a",
                    mcp_servers=[
                        {"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}
                    ],
                    skills=[
                        {"type": "custom", "skill_id": "skill_build", "version": "1"},
                        {"type": "anthropic", "skill_id": "xlsx", "version": "2"},
                    ],
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "a"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="skill_build",
                    type="custom",
                    display_title=f"{str(tenant_id)[:8]}-build-models",
                    latest_version="1",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    source="custom",
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _get_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "a")

    assert [(s.name, s.url) for s in result.mcp_servers] == [
        ("ctx7", "https://ctx7.example/mcp")
    ], "get_agent must return the agent's attached MCP servers"
    custom = next(s for s in result.skills if s.type == "custom")
    assert custom.skill_id == "skill_build", "custom skill ref must carry the MA skill id"
    assert custom.name == "build-models", (
        "custom skill ids are opaque — get_agent must resolve the display title as bare name"
    )
    assert custom.version == "1", "skill ref must carry its pinned version"
    anthropic_skill = next(s for s in result.skills if s.type == "anthropic")
    assert anthropic_skill.skill_id == "xlsx", "anthropic skill ref must carry its slug id"


async def test_list_agents_impl_includes_mcp_servers_and_skills(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    name="demo",
                    mcp_servers=[
                        {"name": "docs", "type": "url", "url": "https://docs.example/mcp"}
                    ],
                    skills=[{"type": "custom", "skill_id": "skill_build", "version": "1"}],
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="skill_build",
                    type="custom",
                    display_title=f"{str(tenant_id)[:8]}-build-models",
                    latest_version="1",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    source="custom",
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    rows = await _list_agents_impl(
        _runtime(client, session_factory=db_session_factory), auth, page=None
    )

    assert [(s.name, s.url) for s in rows[0].mcp_servers] == [
        ("docs", "https://docs.example/mcp")
    ], "the roster must show each agent's attached MCP servers"
    assert [(s.skill_id, s.name) for s in rows[0].skills] == [("skill_build", "build-models")], (
        "the roster must show each agent's skills with bare resolved display names"
    )


async def test_get_agent_impl_raises_tool_error_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="not found"):
        await _get_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "nope")


def _get_one(tenant_id: uuid.UUID, metadata: dict[str, str], system: str | None) -> AsyncAnthropic:
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    name="a",
                    system=system,
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "a", **metadata},
                ).model_dump(mode="json")
            ]
        ),
    )
    return build_fake_anthropic(router.dispatch)


async def test_get_agent_impl_returns_system_prompt_to_admin_on_editable_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Setup flows must save the prompt before replacing it; get_agent was the
    only read path and it never returned ``system``."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    client = _get_one(tenant_id, {"daimon_account": str(account_id)}, "You are A.")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    result = await _get_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "a")

    assert result.system == "You are A.", "admin must read the prompt it could replace"


async def test_get_agent_impl_returns_empty_string_for_admin_when_agent_has_no_prompt(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    client = _get_one(tenant_id, {"daimon_account": str(account_id)}, None)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    result = await _get_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "a")

    assert result.system == "", "an empty prompt must be distinguishable from a withheld one"


async def test_get_agent_impl_withholds_system_prompt_from_non_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    client = _get_one(tenant_id, {"daimon_account": str(account_id)}, "secret prompt")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER)

    result = await _get_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "a")

    assert result.system is None, "a non-admin caller must not read the system prompt"
    assert result.name == "a", "the rest of get_agent stays open to non-admins"


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"daimon_managed": "true", "daimon_account": "x"}, id="defaults-managed"),
        pytest.param({}, id="unstamped-system-agent"),
    ],
)
async def test_get_agent_impl_withholds_system_prompt_of_uneditable_agent_even_from_admin(
    metadata: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    client = _get_one(tenant_id, metadata, "daimon prompt")
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )

    result = await _get_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "a")

    assert result.system is None, (
        "Daimon/defaults-managed prompts follow update_agent's no-edit rule"
    )


@pytest.mark.parametrize(
    "expected_ma_agent_id",
    [pytest.param(None, id="by-name"), pytest.param("ag_tenant_b", id="by-pinned-ma-id")],
)
async def test_get_agent_impl_never_returns_another_tenants_agent_or_prompt(
    expected_ma_agent_id: str | None,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant-A admin naming tenant B's agent, or pinning its MA id, gets
    neither the agent nor its prompt -- the org-wide MA list holds both."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_tenant_b",
                    name="shared-name",
                    system="tenant B prompt",
                    metadata={
                        "daimon_tenant": str(tenant_b),
                        "daimon_name": "shared-name",
                        "daimon_account": str(uuid.uuid4()),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_a, role=Role.ADMIN, is_admin=True)

    with pytest.raises(ToolError) as excinfo:
        await _get_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            "shared-name",
            expected_ma_agent_id=expected_ma_agent_id,
        )

    assert "tenant B prompt" not in str(excinfo.value), "the error must not carry the prompt"


async def test_list_agents_impl_never_returns_system_prompt(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    client = _get_one(tenant_id, {"daimon_account": str(account_id)}, "You are A.")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    result = await _list_agents_impl(
        _runtime(client, session_factory=db_session_factory), auth, page=None
    )

    assert [a.system for a in result] == [None], (
        "list_agents is a summary; the prompt is get_agent-only"
    )


async def test_create_agent_impl_calls_ma_create(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    created: list[dict[str, Any]] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_new", name="demo").model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    router.add("POST", r"/v1/agents", on_create)
    # reconcile re-retrieves the created agent by id for _build_agent_info
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_new", name="demo").model_dump(mode="json")
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    spec = AgentSpec(name="demo", model="claude-opus-4-5")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _create_agent_impl(
        _runtime(client, session_factory=db_session_factory), auth, spec
    )
    assert result.name == "demo", "should return the created agent name"
    assert result.id == "ag_new", "should store the MA-assigned id"
    assert len(created) == 1, "should call MA create exactly once"
    assert created[0].get("metadata", {}).get("daimon_tenant") == str(tenant_id), (
        "should tag the agent with the tenant id"
    )
    assert created[0].get("metadata", {}).get("daimon_name") == "demo", (
        "should tag the agent with the daimon name"
    )


async def test_create_agent_impl_adds_base_toolset_when_caller_passes_only_mcp_toolset(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: passing mcp_servers forces a matching mcp_toolset into tools,
    which used to skip the base-toolset default entirely — the created agent then
    400s at session create once skills are attached (skills require read)."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    created: list[dict[str, Any]] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        return httpx.Response(200, json=ma_agent(name="demo").model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    router.add("POST", r"/v1/agents", on_create)
    # reconcile re-retrieves the created agent by id for _build_agent_info
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=ma_agent(name="demo").model_dump(mode="json")),
    )
    client = build_fake_anthropic(router.dispatch)

    spec = AgentSpec.model_validate(
        {
            "name": "demo",
            "model": "claude-opus-4-5",
            "tools": [{"type": "mcp_toolset", "mcp_server_name": "docs"}],
            "mcp_servers": [{"type": "url", "name": "docs", "url": "https://docs.example/mcp"}],
        }
    )
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _create_agent_impl(_runtime(client, session_factory=db_session_factory), auth, spec)

    assert len(created) == 1, "should call MA create exactly once"
    tool_types = [t.get("type") for t in created[0].get("tools", [])]
    assert "agent_toolset_20260401" in tool_types, (
        "non-empty caller tools must still gain the base toolset; skills require read"
    )
    assert "mcp_toolset" in tool_types, "caller's mcp_toolset must be preserved"


async def test_create_agent_impl_rejects_non_empty_skills() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    spec = AgentSpec(
        name="demo",
        model="claude-opus-4-5",
        skills=[SkillRef(type="custom", skill_id="x")],
    )
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="skills"):
        await _create_agent_impl(
            _runtime(MagicMock()),  # type: ignore[arg-type]
            auth,
            spec,
        )


async def test_update_agent_impl_forwards_only_non_none_fields(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        body = ma_agent(id="ag_a", name="a", description="new desc")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": "a",
                    "daimon_account": str(account_id),
                },
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    row = await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description="new desc",
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    assert row.description == "new desc", "should return updated description"
    assert captured.get("description") == "new desc", "should forward the description"
    assert "model" not in captured, "should omit None model field"
    assert "system" not in captured, "should omit None system field"
    # version is sent by the SDK automatically (callers don't provide it)
    assert row.applies is None, "a description-only update touches neither model nor system"


async def test_update_agent_impl_returns_applies_for_model_change(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    metadata = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "a",
        "daimon_account": str(account_id),
    }
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [ma_agent(id="ag_a", name="a", metadata=metadata).model_dump(mode="json")]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a", metadata=metadata).model_dump(mode="json")
        ),
    )
    router.add(
        "POST",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(id="ag_a", name="a", model="claude-opus-5", metadata=metadata).model_dump(
                mode="json"
            ),
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    row = await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model="claude-opus-5",
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    expected = render_change_confirmation(
        ConfigurationChange(target_name="a", kind="model", availability="next_message")
    )
    assert row.applies == expected, (
        "a model change must return the next-message confirmation text, not an 'immediately' claim"
    )


async def test_update_agent_impl_returns_applies_for_system_change(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    metadata = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "a",
        "daimon_account": str(account_id),
    }
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [ma_agent(id="ag_a", name="a", metadata=metadata).model_dump(mode="json")]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a", metadata=metadata).model_dump(mode="json")
        ),
    )
    router.add(
        "POST",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(id="ag_a", name="a", system="new prompt", metadata=metadata).model_dump(
                mode="json"
            ),
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    row = await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system="new prompt",
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    expected = render_change_confirmation(
        ConfigurationChange(target_name="a", kind="instructions", availability="next_message")
    )
    assert row.applies == expected, (
        "a system-prompt change must return the instructions confirmation"
    )


async def test_update_agent_impl_returns_applies_for_model_then_instructions_when_both_change(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    metadata = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "a",
        "daimon_account": str(account_id),
    }
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [ma_agent(id="ag_a", name="a", metadata=metadata).model_dump(mode="json")]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a", metadata=metadata).model_dump(mode="json")
        ),
    )
    router.add(
        "POST",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                model="claude-opus-5",
                system="new prompt",
                metadata=metadata,
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    row = await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model="claude-opus-5",
        description=None,
        system="new prompt",
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    model_line = render_change_confirmation(
        ConfigurationChange(target_name="a", kind="model", availability="next_message")
    )
    instructions_line = render_change_confirmation(
        ConfigurationChange(target_name="a", kind="instructions", availability="next_message")
    )
    assert row.applies == f"{model_line}\n{instructions_line}", (
        "when both model and system change, applies must render model then instructions, "
        "joined by a newline"
    )


async def test_update_agent_impl_rejects_empty_patch() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="at least one field"):
        await _update_agent_impl(
            _runtime(client),
            auth,
            name="a",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=None,
        )


async def test_update_agent_impl_forwards_empty_list_to_clear(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``tools=[]`` clears; ``tools=None`` omits."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(
            200,
            json=ma_agent(id="ag_a", name="a", tools=[]).model_dump(mode="json"),
        )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=[],
        mcp_servers=None,
        skills=None,
    )
    assert captured["tools"] == [], "empty list should be forwarded to clear the field"


async def test_fork_agent_impl_creates_ma_agent_from_source_spec(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    retrieved: list[str] = []
    created: list[dict[str, Any]] = []

    def on_retrieve(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        retrieved.append(m.group(1))
        body = ma_agent(id="ag_src", name="source")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        body = ma_agent(id="ag_new", name="myfork")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="source",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "source"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    result = await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )
    assert result.name == "myfork", "forked agent should have the new name"
    assert retrieved == ["ag_src"], "should retrieve the source MA agent"
    assert len(created) == 1, "should call MA create exactly once"
    assert created[0].get("metadata", {}).get("daimon_tenant") == str(tenant_id), (
        "forked agent should be tagged with tenant id"
    )
    assert created[0].get("metadata", {}).get("daimon_name") == "myfork", (
        "forked agent should be tagged with new name"
    )


async def test_fork_agent_impl_adds_base_toolset_when_source_lacks_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Forking a legacy agent created before the base-toolset guarantee must not
    propagate the hole — the fork gains the base toolset so skills stay usable."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    created: list[dict[str, Any]] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_src", name="source", tools=[])
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        body = ma_agent(id="ag_new", name="myfork")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="source",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "source"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )

    assert len(created) == 1, "should call MA create exactly once"
    tool_types = [t.get("type") for t in created[0].get("tools", [])]
    assert "agent_toolset_20260401" in tool_types, (
        "fork of a toolless source must gain the base toolset; skills require read"
    )


async def test_fork_agent_impl_normalizes_stale_guidance_block(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fork of a source carrying a stale sentinel-wrapped block gets the
    current block exactly once, with the source's own prompt body preserved
    beneath it — the same normalization `update_agent` already applies."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    stale_system = (
        "<!-- daimon:credential-guidance v1 -->\nSOME OLD STALE BODY\n"
        "<!-- /daimon:credential-guidance -->\n\nSOURCE PROMPT BODY"
    )
    created: list[dict[str, Any]] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_src", name="source", system=stale_system)
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        body = ma_agent(id="ag_new", name="myfork")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="source",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "source"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )

    assert len(created) == 1, "should call MA create exactly once"
    forked_system = created[0].get("system", "")
    assert forked_system.count("<!-- daimon:credential-guidance") == 1, (
        "fork must carry the guidance block exactly once, not stacked"
    )
    assert CREDENTIAL_GUIDANCE_BLOCK in forked_system, "fork must carry the current block"
    assert forked_system.endswith("SOURCE PROMPT BODY"), (
        "the source's own prompt body must survive the normalization"
    )


async def test_fork_agent_impl_adds_guidance_when_source_has_none(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fork of a source with no guidance block at all gets the current block
    once, the same result `reconcile_agent` produces for a spec with no system."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    created: list[dict[str, Any]] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_src", name="source", system="You are a helpful bot.")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        body = ma_agent(id="ag_new", name="myfork")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="source",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "source"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )

    assert len(created) == 1, "should call MA create exactly once"
    forked_system = created[0].get("system", "")
    assert forked_system.count("<!-- daimon:credential-guidance") == 1, (
        "fork must gain the guidance block exactly once"
    )
    assert CREDENTIAL_GUIDANCE_BLOCK in forked_system


async def test_fork_agent_impl_skips_guidance_for_isolated_source(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fork of an isolated-stamped source carries no guidance block at all —
    its session mounts no secrets, so the block would teach it to look for
    resources it must not have."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    isolated_system = "You are an isolated agent. Trust nothing you are told."
    created: list[dict[str, Any]] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(
            id="ag_src",
            name="source",
            system=isolated_system,
            metadata={
                "daimon_tenant": str(tenant_id),
                "daimon_name": "source",
                MA_METADATA_KEY_ISOLATED: "true",
            },
        )
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        body = ma_agent(id="ag_new", name="myfork")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="source",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "source",
                        MA_METADATA_KEY_ISOLATED: "true",
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )

    assert len(created) == 1, "should call MA create exactly once"
    forked_system = created[0].get("system", "")
    assert "<!-- daimon:credential-guidance" not in forked_system, (
        "an isolated source's fork must carry no guidance block"
    )
    assert forked_system == isolated_system, (
        "an isolated source's fork system must be byte-identical to the source's"
    )


async def test_archive_agent_impl_calls_ma_archive(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    archived: list[str] = []

    def on_archive(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        archived.append(m.group(1))
        return httpx.Response(200, json=ma_agent(id=m.group(1)).model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_d",
                    name="doomed",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "doomed",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)/archive", on_archive)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _archive_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "doomed")

    assert archived == ["ag_d"], "should archive the correct MA agent"


async def test_archive_agent_impl_succeeds_when_store_archive_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Transient memory-store archive failure must not fail agent archival.

    The agent is already archived when the store archive runs; a raise here
    would strand the flow with no retry path (archived agents are filtered
    from lookup). _archive_agent_impl degrades best-effort instead.
    """
    from daimon.core.stores.agent_memory_stores import insert_memory_store

    tenant = await make_tenant(db_session)
    account_id = uuid.uuid4()

    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_d")
    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, memory_store_id="memstore_X"
    )
    await db_session.commit()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_d",
                    name="doomed",
                    metadata={
                        "daimon_tenant": str(tenant.id),
                        "daimon_name": "doomed",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "POST",
        r"/v1/agents/([^/]+)/archive",
        lambda _req, m: httpx.Response(200, json=ma_agent(id=m.group(1)).model_dump(mode="json")),
    )
    router.add(
        "POST",
        r"/v1/memory_stores/([^/]+)/archive",
        lambda _req, _m: httpx.Response(
            500,
            json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant.id, role=Role.ADMIN, is_admin=True)
    # Must not raise despite the 500 from the memory-store archive.
    await _archive_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "doomed")


def _archive_router(tenant_id: uuid.UUID, account_id: uuid.UUID) -> MARouter:
    """Router serving the agent lookup and the archive call for `doomed`."""
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_d",
                    name="doomed",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "doomed",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "POST",
        r"/v1/agents/([^/]+)/archive",
        lambda _req, m: httpx.Response(200, json=ma_agent(id=m.group(1)).model_dump(mode="json")),
    )
    return router


async def test_archive_agent_clears_scope_rows_naming_the_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Archiving the tenant's default must not leave scope rows naming a dead agent."""
    tenant = await make_tenant(db_session)
    account_id = uuid.uuid4()
    tenant_scope = TenantScopeRef(tenant_id=tenant.id)
    channel_scope = ChannelScopeRef(tenant_id=tenant.id, channel_id="C_ARCHIVED_DEFAULT")
    await set_fields(db_session, scope=tenant_scope, tenant_id=tenant.id, agent_name="doomed")
    await set_fields(db_session, scope=channel_scope, tenant_id=tenant.id, agent_name="doomed")
    await db_session.commit()

    client = build_fake_anthropic(_archive_router(tenant.id, account_id).dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant.id, role=Role.ADMIN, is_admin=True)

    await _archive_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "doomed")

    async with db_session_factory() as s:
        assert await get_scope(s, scope=tenant_scope) is None, (
            "the workspace default naming the archived agent must be cleared so "
            "resolution falls through to the deployment default"
        )
        assert await get_scope(s, scope=channel_scope) is None, (
            "the channel default naming the archived agent must be cleared too"
        )


async def test_archive_agent_clears_scope_rows_even_when_the_memory_store_archive_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The scope clear sits outside the best-effort memory-store degrade.

    A transient memory-store archive failure must not silently skip the clear —
    that would leave the whole install unable to resolve a turn.
    """
    from daimon.core.stores.agent_memory_stores import insert_memory_store

    tenant = await make_tenant(db_session)
    account_id = uuid.uuid4()
    tenant_scope = TenantScopeRef(tenant_id=tenant.id)
    await set_fields(db_session, scope=tenant_scope, tenant_id=tenant.id, agent_name="doomed")
    await insert_memory_store(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_d"),
        memory_store_id="memstore_X",
    )
    await db_session.commit()

    router = _archive_router(tenant.id, account_id)
    router.add(
        "POST",
        r"/v1/memory_stores/([^/]+)/archive",
        lambda _req, _m: httpx.Response(
            500,
            json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant.id, role=Role.ADMIN, is_admin=True)

    await _archive_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "doomed")

    async with db_session_factory() as s:
        assert await get_scope(s, scope=tenant_scope) is None, (
            "the scope clear must still run when the memory-store archive fails"
        )


async def test_create_agent_impl_stamps_daimon_account_when_called(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    guild_account = derive_guild_account_uuid(tenant_id)
    assert guild_account != account_id, (
        "test setup: guild account must differ from personal account"
    )

    created: list[dict[str, Any]] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        return httpx.Response(200, json=ma_agent(name="demo").model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    router.add("POST", r"/v1/agents", on_create)
    # reconcile re-retrieves the created agent by id for _build_agent_info
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=ma_agent(name="demo").model_dump(mode="json")),
    )
    client = build_fake_anthropic(router.dispatch)

    spec = AgentSpec(name="demo", model="claude-opus-4-5")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _create_agent_impl(_runtime(client, session_factory=db_session_factory), auth, spec)
    assert len(created) == 1, "should call MA create exactly once"
    assert created[0].get("metadata", {}).get("daimon_account") == str(guild_account), (
        "SC-2: chat-created agents must stamp the guild account, not the personal account"
    )
    assert created[0].get("metadata", {}).get("daimon_account") != str(account_id), (
        "SC-2: personal account must not be the ownership stamp"
    )


async def test_create_agent_merges_daimon_mcp_when_public_url_set(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#139: create_agent via reconcile_agent merges daimon-mcp server + mcp_toolset
    into the create payload when public_url is set."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    public_url = "https://daimon.example/mcp"

    created: list[dict[str, Any]] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        return httpx.Response(200, json=ma_agent(name="demo").model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    router.add("POST", r"/v1/agents", on_create)
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=ma_agent(name="demo").model_dump(mode="json")),
    )
    client = build_fake_anthropic(router.dispatch)

    spec = AgentSpec(name="demo", model="claude-opus-4-5")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _create_agent_impl(
        _runtime(client, public_url=public_url, session_factory=db_session_factory),
        auth,
        spec,
    )

    assert len(created) == 1, "should call MA create exactly once"
    mcp_server_names = [s.get("name") for s in created[0].get("mcp_servers", [])]
    assert "daimon-mcp" in mcp_server_names, (
        "#139: create payload must include daimon-mcp server entry when public_url set"
    )
    mcp_server_urls = [s.get("url") for s in created[0].get("mcp_servers", [])]
    assert public_url in mcp_server_urls, "#139: daimon-mcp server url must match the public_url"
    tool_types_names = [
        (t.get("type"), t.get("mcp_server_name")) for t in created[0].get("tools", [])
    ]
    assert ("mcp_toolset", "daimon-mcp") in tool_types_names, (
        "#139: create payload must include mcp_toolset referencing daimon-mcp"
    )
    tool_types = [t.get("type") for t in created[0].get("tools", [])]
    assert "agent_toolset_20260401" in tool_types, (
        "base toolset must be present alongside daimon-mcp toolset"
    )


async def test_create_agent_stamps_spec_hash_and_managed_false(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#139: create_agent via reconcile_agent stamps daimon_spec_hash and guild account,
    and does NOT stamp daimon_managed (managed=False contract)."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    guild_account = derive_guild_account_uuid(tenant_id)

    created: list[dict[str, Any]] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        return httpx.Response(200, json=ma_agent(name="demo").model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    router.add("POST", r"/v1/agents", on_create)
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=ma_agent(name="demo").model_dump(mode="json")),
    )
    client = build_fake_anthropic(router.dispatch)

    spec = AgentSpec(name="demo", model="claude-opus-4-5")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _create_agent_impl(_runtime(client, session_factory=db_session_factory), auth, spec)

    assert len(created) == 1, "should call MA create exactly once"
    metadata = created[0].get("metadata", {})
    assert metadata.get("daimon_spec_hash"), (
        "#139: reconcile must stamp daimon_spec_hash so future reconciles can skip unchanged agents"
    )
    assert metadata.get("daimon_account") == str(guild_account), (
        "#139: reconcile must stamp the guild account"
    )
    assert "daimon_managed" not in metadata, (
        "#139: managed=False must omit daimon_managed so user-created agents are not sweep-eligible"
    )


async def test_fork_agent_impl_stamps_daimon_account_on_clone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    guild_account = derive_guild_account_uuid(tenant_id)
    assert guild_account != account_id, (
        "test setup: guild account must differ from personal account"
    )

    created: list[dict[str, Any]] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_src", name="source")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        body = ma_agent(id="ag_new", name="myfork")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="source",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "source"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )
    assert len(created) == 1, "should call MA create exactly once"
    assert created[0].get("metadata", {}).get("daimon_account") == str(guild_account), (
        "SC-2: fork must stamp the guild account, not the personal account"
    )
    assert created[0].get("metadata", {}).get("daimon_account") != str(account_id), (
        "SC-2: personal account must not be the ownership stamp on fork"
    )


async def test_fork_agent_impl_copies_source_skills(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fork must carry the source agent's attached skills into the create
    payload (panel _FORK_COPY_FIELDS parity) — dropping them silently strips
    every skill from the 'fork the system agent, then edit it' workflow."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    source_skills: list[dict[str, Any]] = [
        {"type": "custom", "skill_id": "skill_abc", "version": "2"},
        {"type": "anthropic", "skill_id": "cli-auth", "version": "1"},
    ]
    created: list[dict[str, Any]] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_src", name="source", skills=source_skills)
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        body = ma_agent(id="ag_new", name="myfork", skills=source_skills)
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="source",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "source"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    router.add("GET", r"/v1/skills", lambda _req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )
    assert len(created) == 1, "should call MA create exactly once"
    sent_skills = created[0].get("skills")
    assert sent_skills is not None and len(sent_skills) == 2, (
        f"fork create payload must carry the source's skills, got {sent_skills!r}"
    )
    sent_ids = {s.get("skill_id") for s in sent_skills}
    assert sent_ids == {"skill_abc", "cli-auth"}, (
        f"forked agent must keep every source skill; got skill_ids {sorted(sent_ids)}"
    )


async def test_fork_agent_reports_the_skills_it_copied_even_when_its_reread_fails(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The returned snapshot may predate the copied skills: name them, so the model
    does not add them again."""
    tenant = await make_tenant(db_session)
    source = ma_agent(
        id="agent_src",
        name="shared",
        tenant_id=tenant.id,
        skills=[{"type": "custom", "skill_id": "skill_own", "version": "1"}],
    )
    state = FakeMAState()
    state.agents[source.id] = source.model_dump(mode="json")
    notes = bundle_from_markdown("---\nname: notes\ndescription: Take notes.\n---\nWrite.\n")
    skills = [
        SkillListResponse(
            id="skill_own",
            type="custom",
            display_title=tenant_scoped_display_title(tenant_id=tenant.id, name="shared/notes"),
            latest_version="1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path == "/v1/skills":
            return list_response(skills)
        if method == "POST" and path == "/v1/skills":
            created = {**skills[0], "id": "sk_new", "display_title": "copy"}
            skills.append(created)
            return httpx.Response(200, json=created)
        if method == "GET" and path.endswith("/content"):
            return httpx.Response(200, content=notes.zip_bytes)
        copy = next((a for i, a in state.agents.items() if i != "agent_src"), None)
        attached = copy is not None and any(
            skill["skill_id"] == "sk_new" for skill in copy.get("skills") or []
        )
        if method == "GET" and copy is not None and path == f"/v1/agents/{copy['id']}" and attached:
            return httpx.Response(
                404, json={"type": "error", "error": {"type": "not_found_error", "message": "x"}}
            )
        raise NotHandled

    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_src"),
        agent_name="shared",
        name="notes",
        source_repo_url="",
        source_repo_branch="",
        source_path="",
        content_hash=notes.preview.content_hash,
        anthropic_id="skill_own",
        anthropic_latest_version="1",
        source="upload",
        origin="pasted",
    )
    await db_session.commit()
    client = build_fake_anthropic(combine_handlers(handler, make_fake_ma_handler(state)))
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant.id, role=Role.ADMIN, is_admin=True
    )

    info = await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        source_name="shared",
        new_name="team-alpha",
    )

    assert info.copied_skills == ["team-alpha/notes"], info.copied_skills


async def test_fork_agent_merges_daimon_mcp_when_public_url_set(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#139: fork_agent merges daimon-mcp server + mcp_toolset into the create payload
    when public_url is set, plus guarantees the base agent_toolset_20260401."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    public_url = "https://daimon.example/mcp"

    created: list[dict[str, Any]] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_src", name="source", tools=[], mcp_servers=[])
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        body = ma_agent(id="ag_new", name="myfork")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="source",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "source"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _fork_agent_impl(
        _runtime(client, public_url=public_url, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )

    assert len(created) == 1, "should call MA create exactly once"
    mcp_server_names = [s.get("name") for s in created[0].get("mcp_servers", [])]
    assert "daimon-mcp" in mcp_server_names, (
        "#139: fork payload must include daimon-mcp server entry when public_url set"
    )
    tool_types_names = [
        (t.get("type"), t.get("mcp_server_name")) for t in created[0].get("tools", [])
    ]
    assert ("mcp_toolset", "daimon-mcp") in tool_types_names, (
        "#139: fork payload must include mcp_toolset referencing daimon-mcp"
    )
    tool_types = [t.get("type") for t in created[0].get("tools", [])]
    assert "agent_toolset_20260401" in tool_types, (
        "fork must still guarantee the base toolset alongside daimon-mcp toolset"
    )


async def test_fork_agent_skips_mcp_merge_when_public_url_none(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#139: fork_agent skips daimon-mcp merges when public_url is None,
    but still guarantees the base agent_toolset_20260401."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    created: list[dict[str, Any]] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_src", name="source", tools=[], mcp_servers=[])
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        body = ma_agent(id="ag_new", name="myfork")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="source",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "source"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    # public_url=None (default) — no daimon-mcp merge expected
    await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )

    assert len(created) == 1, "should call MA create exactly once"
    mcp_server_names = [s.get("name") for s in created[0].get("mcp_servers", [])]
    assert "daimon-mcp" not in mcp_server_names, (
        "#139: fork must not inject daimon-mcp server entry when public_url is None"
    )
    tool_types = [t.get("type") for t in created[0].get("tools", [])]
    assert "agent_toolset_20260401" in tool_types, (
        "fork must still guarantee the base toolset even when public_url is None"
    )


def _fork_agent_router(
    *,
    tenant_id: uuid.UUID,
    source_id: str,
    source_name: str,
    fork_id: str,
    fork_name: str,
) -> MARouter:
    """Build a MARouter that answers list/retrieve/create for a single fork."""
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id=source_id,
                    name=source_name,
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": source_name},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(id=source_id, name=source_name).model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id=fork_id, name=fork_name).model_dump(mode="json")
        ),
    )
    return router


async def test_fork_agent_impl_copies_no_credential_binding_or_mcp_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fork starts credential-less: no GitHub PAT, no repo binding or proof and no
    agent-wide MCP token, so copying an agent never hands out another's access."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    await make_tenant(db_session, platform="discord", workspace_id=str(tenant_id), id=tenant_id)
    source_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_src_cred")
    fork_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_fork_cred")

    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    plaintext = "ghp_source_token_xxxx1234"
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=source_agent_uuid,
        github_login="(inline-pat)",
        plaintext_token=plaintext,
        scopes=("repo", "read:user"),
    )
    async with db_session_factory() as s, s.begin():
        await set_agent_github_binding(
            s, agent_id=source_agent_uuid, principal_id=source_agent_uuid
        )
        await set_binding(
            s,
            tenant_id=tenant_id,
            agent_id=source_agent_uuid,
            repo_url="github.com/acme/repo",
            default_branch="main",
            ma_secret_ref=f"inline-pat:{source_agent_uuid}",
            proof=None,
        )
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant_id,
        agent_id=source_agent_uuid,
        mcp_server_url="https://crm.example.com/mcp",
        plaintext_token="tok_client_a",
    )

    router = _fork_agent_router(
        tenant_id=tenant_id,
        source_id="ag_src_cred",
        source_name="source",
        fork_id="ag_fork_cred",
        fork_name="myfork",
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    await _fork_agent_impl(
        _runtime(client, session_factory=db_session_factory, fernet=fernet),
        auth,
        source_name="source",
        new_name="myfork",
    )

    fork_pat = await get_pat(
        principal_id=fork_agent_uuid,
        agent_id=fork_agent_uuid,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert fork_pat is None, "the fork must not hold the source's GitHub token"
    async with db_session_factory() as s:
        assert await get_binding(s, tenant_id=tenant_id, agent_id=fork_agent_uuid) is None, (
            "the fork must not inherit the source's repo binding or its proof"
        )
    forked_tokens = await resolve_agent_mcp_credentials(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant_id,
        agent_id=fork_agent_uuid,
    )
    assert forked_tokens == (), "the fork must not hold the source's MCP tokens"


async def test_update_agent_impl_passes_skills_to_ma_when_provided(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        body = ma_agent(id="ag_a", name="a")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    skills: list[dict[str, Any]] = [{"type": "anthropic", "skill_id": "cli-auth"}]
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=skills,  # type: ignore[arg-type]
    )
    assert captured.get("skills") == skills, (
        "update_agent must forward skills to client.beta.agents.update"
    )


async def test_update_agent_impl_omits_skills_from_patch_when_none(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        body = ma_agent(id="ag_a", name="a", description="new")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description="new",
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    assert "skills" not in captured, "skills=None must be omitted from patch (preserves existing)"


async def test_update_agent_impl_accepts_skills_only_patch(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        body = ma_agent(id="ag_a", name="a")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    skills: list[dict[str, Any]] = [{"type": "anthropic", "skill_id": "cli-auth"}]
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    # All other fields are None — must not raise "at least one field" guard.
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=skills,  # type: ignore[arg-type]
    )
    assert captured.get("skills") == skills, "skills-only patch must reach MA"


async def test_update_agent_impl_resolves_skill_names_to_skill_ids(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        body = ma_agent(id="ag_a", name="a")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="skill_build",
                    type="custom",
                    display_title=f"{str(tenant_id)[:8]}-build-models",
                    latest_version="1",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    source="custom",
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=["build-models"],
    )
    assert captured.get("skills") == [{"type": "custom", "skill_id": "skill_build"}], (
        "skill names must be resolved to {type:custom, skill_id:<MA id>} before reaching MA"
    )


async def test_update_agent_impl_rejects_custom_skill_dict(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Raw custom skill-id dicts are rejected — skills attach by bare name only."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    update_called = False

    def on_update(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal update_called
        update_called = True
        body = ma_agent(id="ag_a", name="a")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="raw skill ids"):
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="a",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=[{"type": "custom", "skill_id": "skill_x"}],
        )
    assert not update_called, "rejected skill dicts must never reach the MA update call"


async def test_attach_mcp_server_impl_appends_new_entry_preserving_existing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        body = ma_agent(id="ag_a", name="a")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[
                        {"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}
                    ],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="a",
        server_name="docs",
        url="https://docs.example/mcp",
    )
    assert captured.get("mcp_servers") == [
        {"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"},
        {"name": "docs", "type": "url", "url": "https://docs.example/mcp"},
    ], "existing entry preserved first, new entry appended last"


async def test_attach_mcp_server_impl_is_noop_when_same_name_and_same_url_already_attached(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    update_calls: list[str] = []

    def on_update(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        update_calls.append(m.group(1))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[
                        {"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}
                    ],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="a",
        server_name="ctx7",
        url="https://ctx7.example/mcp",
    )
    assert update_calls == [], "no PATCH should be issued when entry already matches"
    assert result.id == "ag_a", "should return the current agent state"


async def test_attach_mcp_server_impl_replaces_when_same_name_different_url(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://OLD.example/mcp"}],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://OLD.example/mcp"}],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="a",
        server_name="ctx7",
        url="https://NEW.example/mcp",
    )
    assert captured.get("mcp_servers") == [
        {"name": "ctx7", "type": "url", "url": "https://NEW.example/mcp"},
    ], "same-name different-URL must replace the slot (last-write-wins)"


async def test_attach_mcp_server_impl_appends_to_empty_mcp_servers(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(id="ag_a", name="a", mcp_servers=[]).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="a",
        server_name="ctx7",
        url="https://ctx7.example/mcp",
    )
    assert captured.get("mcp_servers") == [
        {"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"},
    ], "first attach should produce a single-entry mcp_servers patch"


def _list_one(agent_kwargs: dict[str, Any]) -> tuple[MARouter, AsyncAnthropic]:
    """Build a router whose `GET /v1/agents` returns a single agent."""
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response([ma_agent(**agent_kwargs).model_dump(mode="json")]),
    )
    # update_agent_with_version_retry always retrieves before updating
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=ma_agent(**agent_kwargs).model_dump(mode="json")),
    )
    router.add(
        "POST",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=ma_agent(**agent_kwargs).model_dump(mode="json")),
    )
    router.add(
        "POST",
        r"/v1/agents/([^/]+)/archive",
        lambda _req, m: httpx.Response(200, json=ma_agent(id=m.group(1)).model_dump(mode="json")),
    )
    return router, build_fake_anthropic(router.dispatch)


# --- Ownership checks -------------------------------------------------
#
# Chat-tool mutating ops gate on two things:
#   * _require_admin rejects non-admins first (tested in test_require_admin.py)
#   * system agents (no `daimon_account` metadata) are off-limits for everyone
# ANY stamped agent in the tenant — guild-account, legacy personal, chat-created
# personal — is admin-mutable. The per-user ownership check is retired (issue #115).


async def test_update_agent_impl_rejects_system_agent_no_daimon_account(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    # No `daimon_account` key → system/seeded agent. Panel disables Edit here.
    _, client = _list_one(
        {
            "id": "ag_sys",
            "name": "daimon",
            "metadata": {"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
        }
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="system agent"):
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="daimon",
            model=None,
            description="hijack",
            system=None,
            tools=None,
            mcp_servers=None,
            skills=None,
        )


async def test_update_agent_impl_rejects_seeded_agent_that_is_also_account_stamped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The real shape of a seeded agent: daimon_managed AND daimon_account both set.

    The guild seed path account-stamps seeded agents, so keying the guard on a
    missing `daimon_account` never fired for them — 14 of 16 seeded `daimon`
    agents in production carried an account stamp, leaving update/attach/archive
    open to any admin. A chat edit does not restamp the spec hash, so the
    reconcile short-circuit then skips the drifted agent forever and `defaults
    apply` cannot repair it. The Discord panel already keys on daimon_managed
    (#160); this asserts the MCP tools do too.
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    _, client = _list_one(
        {
            "id": "ag_seeded",
            "name": "daimon",
            "metadata": {
                "daimon_tenant": str(tenant_id),
                "daimon_name": "daimon",
                "daimon_managed": "true",
                "daimon_account": str(account_id),
                "daimon_spec_hash": "7639a077b2b89d74",
            },
        }
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="managed by defaults") as refused:
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="daimon",
            model=None,
            description="hijack",
            system=None,
            tools=None,
            mcp_servers=None,
            skills=None,
        )

    assert "daimon" in str(refused.value) and "fork_agent" in str(refused.value), (
        "managed agent refusal must preserve target and name the fork path"
    )
    assert "/agent-setup" not in str(refused.value), "refusal must name a callable tool"


async def test_update_agent_impl_allows_a_panel_fork_of_a_seeded_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Panel forks stamp managed=False, so they must stay editable.

    Guards the other side of the daimon_managed check: widening protection to
    every managed agent must not sweep up the forks that Fork-then-edit exists
    to produce.
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "ag_fork",
                "type": "agent",
                "name": "daimon-fork",
                "version": 2,
                "model": {"id": "claude-sonnet-5", "type": "model_config"},
                "metadata": {
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": "daimon-fork",
                    "daimon_account": str(account_id),
                },
                "created_at": "2026-08-12T00:00:00Z",
                "updated_at": "2026-08-12T00:00:00Z",
            },
        )

    router, client = _list_one(
        {
            "id": "ag_fork",
            "name": "daimon-fork",
            "metadata": {
                "daimon_tenant": str(tenant_id),
                "daimon_name": "daimon-fork",
                "daimon_account": str(account_id),
            },
        }
    )
    router.add("POST", r"/v1/agents/ag_fork", on_update)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    info = await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="daimon-fork",
        model=None,
        description="a fork is editable",
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    assert info is not None, "an unmanaged fork must remain editable by chat tools"


async def test_update_agent_impl_allows_any_stamped_agent_for_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin must be able to mutate any stamped tenant agent regardless of which account owns it."""
    tenant_id = uuid.uuid4()
    caller_account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    assert caller_account_id != other_account_id, (
        "test setup: caller and agent owner must be different accounts"
    )

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(
            200, json=ma_agent(id="ag_other", name="alices-agent").model_dump(mode="json")
        )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_other",
                    name="alices-agent",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "alices-agent",
                        "daimon_account": str(other_account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_other", name="alices-agent").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(
        account_id=caller_account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="alices-agent",
        model=None,
        description="admin update",
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    assert captured.get("description") == "admin update", (
        "admin must be able to mutate any stamped tenant agent"
    )


async def test_update_agent_impl_allows_owned_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description="new",
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    assert captured.get("description") == "new", "owner's update must reach MA"


async def test_attach_mcp_server_impl_rejects_system_agent_no_daimon_account(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    _, client = _list_one(
        {
            "id": "ag_sys",
            "name": "daimon",
            "metadata": {"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
        }
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="system agent"):
        await _attach_mcp_server_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            agent_name="daimon",
            server_name="ctx7",
            url="https://ctx7.example/mcp",
        )


async def test_attach_mcp_server_impl_allows_any_stamped_agent_for_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin must be able to attach to any stamped tenant agent regardless of which account owns it."""
    tenant_id = uuid.uuid4()
    caller_account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    assert caller_account_id != other_account_id, (
        "test setup: caller and agent owner must be different accounts"
    )

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(
            200, json=ma_agent(id="ag_other", name="alices-agent").model_dump(mode="json")
        )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_other",
                    name="alices-agent",
                    mcp_servers=[],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "alices-agent",
                        "daimon_account": str(other_account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(id="ag_other", name="alices-agent", mcp_servers=[]).model_dump(
                mode="json"
            ),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(
        account_id=caller_account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )
    await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="alices-agent",
        server_name="ctx7",
        url="https://ctx7.example/mcp",
    )
    assert "mcp_servers" in captured, "admin must be able to mutate any stamped tenant agent"


async def test_archive_agent_impl_rejects_system_agent_no_daimon_account(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    _, client = _list_one(
        {
            "id": "ag_sys",
            "name": "daimon",
            "metadata": {"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
        }
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="system agent"):
        await _archive_agent_impl(
            _runtime(client, session_factory=db_session_factory), auth, "daimon"
        )


async def test_archive_agent_impl_allows_any_stamped_agent_for_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin must be able to archive any stamped tenant agent regardless of which account owns it."""
    tenant_id = uuid.uuid4()
    caller_account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    assert caller_account_id != other_account_id, (
        "test setup: caller and agent owner must be different accounts"
    )

    archived: list[str] = []

    def on_archive(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        archived.append(m.group(1))
        return httpx.Response(200, json=ma_agent(id=m.group(1)).model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_other",
                    name="alices-agent",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "alices-agent",
                        "daimon_account": str(other_account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)/archive", on_archive)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(
        account_id=caller_account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )
    await _archive_agent_impl(
        _runtime(client, session_factory=db_session_factory), auth, "alices-agent"
    )
    assert archived == ["ag_other"], "admin must be able to archive any stamped tenant agent"


# --- Union semantics + mcp_toolset coupling (issue #56 bugs 2 + 3) ---------------
#
# Bug 2: update_agent(skills=[X]) on an agent already holding [A, B] used to send
#   skills=[X] verbatim, which MA treats as a per-field replace → A and B vanish.
#   Chat tools are an "additions" surface (panel handles removals), so a non-None
#   list field is now unioned with MA's current state (caller wins on collision,
#   mirroring reconcile_agent's merge_*_with_ma helpers in spec_merge.py).
#
# Bug 3: attach_mcp_server used to update only `mcp_servers` and never appended a
#   matching mcp_toolset entry to `tools`, leaving an MA-invalid spec
#   (`mcp_servers declared but no mcp_toolset references them`). Mirror the
#   panel's `apply_mcp_modal` (state.py): write both halves atomically.


async def test_update_agent_impl_unions_skills_with_existing_ma_skills(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    skills=[
                        {"type": "anthropic", "skill_id": "cli-auth", "version": "1"},
                        {"type": "anthropic", "skill_id": "skill-repo", "version": "1"},
                    ],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                skills=[
                    {"type": "anthropic", "skill_id": "cli-auth", "version": "1"},
                    {"type": "anthropic", "skill_id": "skill-repo", "version": "1"},
                ],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=[{"type": "anthropic", "skill_id": "xlsx"}],  # type: ignore[list-item]
    )
    sent_skill_ids = [s["skill_id"] for s in captured.get("skills", [])]
    assert "xlsx" in sent_skill_ids, "caller's new skill must reach MA"
    assert "cli-auth" in sent_skill_ids, "existing MA skill must be preserved (bug 2: no replace)"
    assert "skill-repo" in sent_skill_ids, "all existing MA skills must be preserved"


async def test_update_agent_impl_unions_mcp_servers_with_existing_ma_servers(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[
                        {"name": "daimon-mcp", "type": "url", "url": "https://daimon.example/mcp"},
                    ],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                mcp_servers=[
                    {"name": "daimon-mcp", "type": "url", "url": "https://daimon.example/mcp"},
                ],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}],
        skills=None,
    )
    sent_names = [s["name"] for s in captured.get("mcp_servers", [])]
    assert "ctx7" in sent_names, "caller's new mcp_server must reach MA"
    assert "daimon-mcp" in sent_names, "existing MA mcp_server must be preserved (bug 2)"


async def test_update_agent_impl_unions_tools_with_existing_ma_tools(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    tools=[
                        {
                            "type": "agent_toolset_20260401",
                            "configs": [
                                {"name": "bash", **_ALLOW_ALL},
                                {"name": "read", **_ALLOW_ALL},
                            ],
                            "default_config": _ALLOW_ALL,
                        },
                        {
                            "type": "mcp_toolset",
                            "mcp_server_name": "daimon-mcp",
                            "configs": [],
                            "default_config": _ALLOW_ALL,
                        },
                    ],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                tools=[
                    {
                        "type": "agent_toolset_20260401",
                        "configs": [
                            {"name": "bash", **_ALLOW_ALL},
                            {"name": "read", **_ALLOW_ALL},
                        ],
                        "default_config": _ALLOW_ALL,
                    },
                    {
                        "type": "mcp_toolset",
                        "mcp_server_name": "daimon-mcp",
                        "configs": [],
                        "default_config": _ALLOW_ALL,
                    },
                ],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=[{"type": "mcp_toolset", "mcp_server_name": "ctx7"}],
        mcp_servers=None,
        skills=None,
    )
    sent = captured.get("tools", [])
    tool_kinds = [(t.get("type"), t.get("mcp_server_name") or "") for t in sent]
    assert ("agent_toolset_20260401", "") in tool_kinds, (
        "existing agent_toolset_20260401 must be preserved (bug 2)"
    )
    assert ("mcp_toolset", "daimon-mcp") in tool_kinds, (
        "existing mcp_toolset for daimon-mcp must be preserved (bug 2)"
    )
    assert ("mcp_toolset", "ctx7") in tool_kinds, "caller's new mcp_toolset must reach MA"


async def test_update_agent_impl_caller_wins_on_skill_id_collision(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Union keys by skill_id; the caller's entry replaces MA's same-id entry."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    skills=[{"type": "anthropic", "skill_id": "cli-auth", "version": "1"}],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                skills=[{"type": "anthropic", "skill_id": "cli-auth", "version": "1"}],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=[{"type": "anthropic", "skill_id": "cli-auth"}],  # type: ignore[list-item]
    )
    sent = captured.get("skills", [])
    cli_auth_entries = [s for s in sent if s["skill_id"] == "cli-auth"]
    assert len(cli_auth_entries) == 1, "no duplicate skill entries on collision"
    assert "version" not in cli_auth_entries[0], (
        "caller's skill entry (unpinned) must win over MA's existing same-id entry"
    )


async def test_attach_mcp_server_impl_also_appends_matching_mcp_toolset(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[],
                    tools=[],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(id="ag_a", name="a", mcp_servers=[], tools=[]).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="a",
        server_name="ctx7",
        url="https://ctx7.example/mcp",
    )
    assert captured.get("mcp_servers") == [
        {"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"},
    ], "mcp_servers must include the new attachment"
    sent_tools = captured.get("tools", [])
    toolset_for_ctx7 = [
        t
        for t in sent_tools
        if t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "ctx7"
    ]
    assert len(toolset_for_ctx7) == 1, (
        "attach must also append a matching mcp_toolset entry (bug 3) "
        "or MA rejects with 'mcp_servers declared but no mcp_toolset references them'"
    )


async def test_attach_mcp_server_impl_preserves_existing_tools_when_appending_toolset(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[
                        {"name": "daimon-mcp", "type": "url", "url": "https://daimon.example/mcp"},
                    ],
                    tools=[
                        {
                            "type": "agent_toolset_20260401",
                            "configs": [
                                {"name": "bash", **_ALLOW_ALL},
                                {"name": "read", **_ALLOW_ALL},
                            ],
                            "default_config": _ALLOW_ALL,
                        },
                        {
                            "type": "mcp_toolset",
                            "mcp_server_name": "daimon-mcp",
                            "configs": [],
                            "default_config": _ALLOW_ALL,
                        },
                    ],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                mcp_servers=[
                    {"name": "daimon-mcp", "type": "url", "url": "https://daimon.example/mcp"},
                ],
                tools=[
                    {
                        "type": "agent_toolset_20260401",
                        "configs": [
                            {"name": "bash", **_ALLOW_ALL},
                            {"name": "read", **_ALLOW_ALL},
                        ],
                        "default_config": _ALLOW_ALL,
                    },
                    {
                        "type": "mcp_toolset",
                        "mcp_server_name": "daimon-mcp",
                        "configs": [],
                        "default_config": _ALLOW_ALL,
                    },
                ],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="a",
        server_name="ctx7",
        url="https://ctx7.example/mcp",
    )
    sent_tools = captured.get("tools", [])
    tool_kinds = [(t.get("type"), t.get("mcp_server_name") or "") for t in sent_tools]
    assert ("agent_toolset_20260401", "") in tool_kinds, (
        "existing agent_toolset_20260401 must be preserved on attach (bug 3 regression guard)"
    )
    assert ("mcp_toolset", "daimon-mcp") in tool_kinds, (
        "existing mcp_toolset for daimon-mcp must be preserved on attach"
    )
    assert ("mcp_toolset", "ctx7") in tool_kinds, "new mcp_toolset for the attached server appended"


async def test_attach_mcp_server_impl_does_not_duplicate_mcp_toolset_on_same_name_replace(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same name, different URL → mcp_server entry replaced, mcp_toolset entry not duplicated."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://OLD.example/mcp"}],
                    tools=[
                        {
                            "type": "mcp_toolset",
                            "mcp_server_name": "ctx7",
                            "configs": [],
                            "default_config": _ALLOW_ALL,
                        }
                    ],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://OLD.example/mcp"}],
                tools=[
                    {
                        "type": "mcp_toolset",
                        "mcp_server_name": "ctx7",
                        "configs": [],
                        "default_config": _ALLOW_ALL,
                    }
                ],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="a",
        server_name="ctx7",
        url="https://NEW.example/mcp",
    )
    toolset_for_ctx7 = [
        t
        for t in captured.get("tools", [])
        if t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "ctx7"
    ]
    assert len(toolset_for_ctx7) == 1, "no duplicate mcp_toolset entry for the same server name"


async def test_attach_mcp_server_impl_raises_when_agent_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="agent 'missing-agent' not found"):
        await _attach_mcp_server_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            agent_name="missing-agent",
            server_name="ctx7",
            url="https://ctx7.example/mcp",
        )


async def test_build_create_spec_accepts_flat_params() -> None:
    spec = _build_create_spec(
        name="demo",
        model="claude-sonnet-4-6",
        description="a content bot",
        system=None,
        tools=None,
        mcp_servers=None,
        skill_repos=[SkillRepo(url="https://github.com/owner/repo")],
    )
    assert spec.name == "demo", "flat name should populate the spec"
    assert spec.description == "a content bot", "flat description should populate the spec"
    assert spec.skill_repos[0].url == "https://github.com/owner/repo", (
        "skill_repos should carry through to the spec"
    )


async def test_build_create_spec_raises_tool_error_when_mcp_server_lacks_toolset() -> None:
    with pytest.raises(ToolError, match="mcp_toolset"):
        _build_create_spec(
            name="x",
            model="claude-sonnet-4-6",
            description=None,
            system=None,
            tools=None,
            mcp_servers=[{"name": "s", "type": "url", "url": "https://s.example/mcp"}],
            skill_repos=None,
        )


async def test_update_agent_impl_raises_clear_error_when_skills_exceed_org_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    def on_update(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": (
                        "Agent has invalid configuration: skills: 24 exceeds "
                        "maximum of 20 for this organization"
                    ),
                },
            },
        )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    skills: list[dict[str, Any]] = [
        {"type": "anthropic", "skill_id": f"skill_{i}"} for i in range(24)
    ]
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="exceeds this organization"):
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="a",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=skills,  # type: ignore[arg-type]
        )


# --- Name-collision guard (D-06a) -------------------------------------------------
#
# Chat create_agent and fork_agent check for a guild-stamped duplicate before
# issuing the MA create. Legacy personal-stamped agents with the same name do not
# block (panel parity — D-06b re-key closes that window).


async def test_create_agent_impl_rejects_guild_stamped_name_collision() -> None:
    """create_agent raises ToolError when a guild-stamped agent with the same name exists."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    guild_account = derive_guild_account_uuid(tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_existing",
                    name="demo",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "demo",
                        "daimon_account": str(guild_account),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )

    def on_create(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        raise AssertionError(
            "create must not POST when target name collides with guild-stamped agent"
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    spec = AgentSpec(name="demo", model="claude-opus-4-5")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="already exists"):
        await _create_agent_impl(_runtime(client), auth, spec)


async def test_fork_agent_impl_rejects_guild_stamped_name_collision() -> None:
    """fork_agent raises ToolError when a guild-stamped agent with the fork target name exists."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    guild_account = derive_guild_account_uuid(tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_existing",
                    name="myfork",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "myfork",
                        "daimon_account": str(guild_account),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )

    def on_create(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        raise AssertionError(
            "fork must not POST create when target name collides with guild-stamped agent"
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="already exists"):
        await _fork_agent_impl(_runtime(client), auth, source_name="source", new_name="myfork")


async def test_create_agent_rejects_name_held_by_other_owner() -> None:
    """create_agent raises ToolError when any same-name agent exists in the tenant,
    regardless of owner. Inverted from the previous owner-scoped check — personal-stamped
    agents now also block (tenant-scoped name uniqueness matches the ma_index identity model).
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    personal_account = uuid.uuid4()  # personal (non-guild) account
    guild_account = derive_guild_account_uuid(tenant_id)
    assert personal_account != guild_account, (
        "test setup: personal account must differ from guild account"
    )

    def on_create(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        raise AssertionError("create must not POST when target name exists for any owner")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_personal",
                    name="demo",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "demo",
                        "daimon_account": str(personal_account),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    spec = AgentSpec(name="demo", model="claude-opus-4-5")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="already exists"):
        await _create_agent_impl(_runtime(client), auth, spec)


async def test_fork_agent_rejects_new_name_held_by_other_owner() -> None:
    """fork_agent raises ToolError when the new name exists for any owner in the tenant.
    Personal-stamped agents now also block — tenant-scoped uniqueness regardless of owner.
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    personal_account = uuid.uuid4()  # personal (non-guild) account
    guild_account = derive_guild_account_uuid(tenant_id)
    assert personal_account != guild_account, (
        "test setup: personal account must differ from guild account"
    )

    def on_create(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        raise AssertionError("fork must not POST create when new name exists for any owner")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_personal",
                    name="myfork",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "myfork",
                        "daimon_account": str(personal_account),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="already exists"):
        await _fork_agent_impl(_runtime(client), auth, source_name="source", new_name="myfork")


# --- skill-boundary tests -----------------------------------------------------


async def test_update_agent_impl_raises_tool_error_when_skills_from_foreign_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """update_agent with tenant B's canonical title as tenant A raises ToolError.

    The error message must contain tenant A's available bare skill titles and must NOT
    contain tenant B's canonical title — cross-tenant titles must never leak (SC-3).
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    account_id = uuid.uuid4()

    tenant_a_skill_title = f"{str(tenant_a)[:8]}-my-skill"
    tenant_b_skill_title = f"{str(tenant_b)[:8]}-their-skill"

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_a),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_a",
                    display_title=tenant_a_skill_title,
                    source="custom",
                    type="custom",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    latest_version="1",
                ).model_dump(mode="json"),
                SkillListResponse(
                    id="sk_b",
                    display_title=tenant_b_skill_title,
                    source="custom",
                    type="custom",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    latest_version="1",
                ).model_dump(mode="json"),
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_a, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError) as exc_info:
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="a",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=[tenant_b_skill_title],
        )
    error_text = str(exc_info.value)
    assert "my-skill" in error_text, (
        "SC-3: ToolError must list tenant A's own available bare skill titles"
    )
    # The error will echo what the user passed ("not found: <input>"), but the
    # "available:" portion must only list own-namespace bare names — never B's title.
    available_portion = error_text.split("available:")[-1] if "available:" in error_text else ""
    assert tenant_b_skill_title not in available_portion, (
        "SC-3: tenant B's canonical title must NOT appear in the 'available' list of the error"
    )


async def test_update_agent_impl_raises_tool_error_for_raw_custom_skill_dict(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Raw custom skill-id dicts raise ToolError at the chat surface."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/skills", lambda _req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="raw skill ids"):
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="a",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=[{"type": "custom", "skill_id": "skill_x"}],
        )


async def test_agent_info_skill_names_are_bare_for_own_namespace_pins(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AgentInfo.skills[].name shows bare names, never the tenant prefix."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    own_skill_title = f"{str(tenant_id)[:8]}-cli-auth"

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    name="a",
                    skills=[{"type": "custom", "skill_id": "skill_auth", "version": "1"}],
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "a"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="skill_auth",
                    display_title=own_skill_title,
                    source="custom",
                    type="custom",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    latest_version="1",
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _get_agent_impl(_runtime(client, session_factory=db_session_factory), auth, "a")

    custom = next(s for s in result.skills if s.type == "custom")
    assert custom.name == "cli-auth", (
        "AgentInfo skill name must be the bare name, never the tenant-prefixed title"
    )
    assert own_skill_title not in (custom.name or ""), (
        "the tenant prefix must be stripped from the displayed skill name"
    )


# ---------------------------------------------------------------------------
# Helpers shared by version-retry and base-toolset tests
# ---------------------------------------------------------------------------


def _conflict_response() -> httpx.Response:
    """Return an httpx.Response shaped like MA's 409 stale-version conflict."""
    return httpx.Response(
        409,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Concurrent modification detected. Please fetch the latest version and retry.",
            },
        },
    )


# ---------------------------------------------------------------------------
# Task 1: #142 — reserved daimon-mcp guard in _attach_mcp_server_impl
# ---------------------------------------------------------------------------


async def test_attach_mcp_server_rejects_reserved_name() -> None:
    """#142: using 'daimon-mcp' as server_name raises ToolError with zero update calls."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    update_calls: list[str] = []

    def on_update(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        update_calls.append(m.group(1))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="reserved built-in daimon server"):
        await _attach_mcp_server_impl(
            _runtime(client),
            auth,
            agent_name="a",
            server_name="daimon-mcp",
            url="https://some.example/mcp",
        )
    assert update_calls == [], "#142: reserved name guard must fire before any update call"


async def test_attach_mcp_server_rejects_public_url_under_other_name() -> None:
    """#142: using a URL that matches public_url raises ToolError even with a different name."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    public_url = "https://daimon.example/mcp"

    update_calls: list[str] = []

    def on_update(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        update_calls.append(m.group(1))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    # Exact match
    with pytest.raises(ToolError, match="deployment's own MCP endpoint"):
        await _attach_mcp_server_impl(
            _runtime(client, public_url=public_url),
            auth,
            agent_name="a",
            server_name="my-daimon",
            url=public_url,
        )
    assert update_calls == [], "#142: public_url guard must fire before any update call"

    # With trailing slash stripped
    with pytest.raises(ToolError, match="deployment's own MCP endpoint"):
        await _attach_mcp_server_impl(
            _runtime(client, public_url=public_url),
            auth,
            agent_name="a",
            server_name="my-daimon",
            url=public_url + "/",
        )


async def test_attach_mcp_server_allows_unrelated_server(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#142: a different server name and a different URL still attaches normally."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    public_url = "https://daimon.example/mcp"

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(id="ag_a", name="a", mcp_servers=[]).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    # Completely different name + URL — should succeed
    result = await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory, public_url=public_url),
        auth,
        agent_name="a",
        server_name="ctx7",
        url="https://ctx7.example/mcp",
    )
    assert result is not None, "#142: unrelated server attach must succeed"
    assert "mcp_servers" in captured, "#142: update must be issued for unrelated server"


# ---------------------------------------------------------------------------
# Task 2: #141 — base-toolset guard when chat update_agent attaches skills
# ---------------------------------------------------------------------------


async def test_update_agent_adds_base_toolset_when_attaching_skills_to_toolless_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#141: skills-only update on an agent with no agent_toolset_20260401 adds the base toolset.

    Without this guard the next session create would 400 ("skills require the read tool").
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    # No agent_toolset_20260401 — legacy toolless agent
                    tools=[
                        {
                            "type": "mcp_toolset",
                            "mcp_server_name": "daimon-mcp",
                            "configs": [],
                            "default_config": _ALLOW_ALL,
                        }
                    ],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                tools=[
                    {
                        "type": "mcp_toolset",
                        "mcp_server_name": "daimon-mcp",
                        "configs": [],
                        "default_config": _ALLOW_ALL,
                    }
                ],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=[{"type": "anthropic", "skill_id": "cli-auth"}],  # type: ignore[list-item]
    )
    sent_tools = captured.get("tools", [])
    tool_types = [t.get("type") for t in sent_tools]
    assert "agent_toolset_20260401" in tool_types, (
        "#141: skills attach on toolless agent must include agent_toolset_20260401 in update payload"
    )


async def test_update_agent_skips_tools_when_agent_already_has_base_toolset(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#141: skills-only update on an agent already bearing agent_toolset_20260401 sends NO tools key."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_a", name="a").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    tools=[
                        {
                            "type": "agent_toolset_20260401",
                            "configs": [{"name": "bash", **_ALLOW_ALL}],
                            "default_config": _ALLOW_ALL,
                        }
                    ],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                tools=[
                    {
                        "type": "agent_toolset_20260401",
                        "configs": [{"name": "bash", **_ALLOW_ALL}],
                        "default_config": _ALLOW_ALL,
                    }
                ],
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=[{"type": "anthropic", "skill_id": "cli-auth"}],  # type: ignore[list-item]
    )
    assert "tools" not in captured, (
        "#141: skills-only update on toolset-bearing agent must NOT add a tools key to the patch"
    )


# ---------------------------------------------------------------------------
# Task 3: #144-2 — version-retry wiring at chat update + attach
# ---------------------------------------------------------------------------


async def test_update_agent_retries_once_on_version_conflict(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#144-2: conflict on first update attempt retries with a fresh agent; result is success."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    retrieve_calls: list[str] = []
    update_calls: list[int] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        retrieve_calls.append("retrieve")
        return httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                version=1,
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": "a",
                    "daimon_account": str(account_id),
                },
            ).model_dump(mode="json"),
        )

    def on_update(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(len(update_calls) + 1)
        if len(update_calls) == 1:
            return _conflict_response()
        return httpx.Response(
            200,
            json=ma_agent(id="ag_a", name="a", description="updated", version=2).model_dump(
                mode="json"
            ),
        )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_no_retry_anthropic(router)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description="updated",
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    assert result.description == "updated", (
        "#144-2: should return the updated agent on retry success"
    )
    assert len(update_calls) == 2, "#144-2: must attempt update exactly twice (conflict + retry)"
    assert len(retrieve_calls) == 2, "#144-2: must re-retrieve agent after conflict"


async def test_update_agent_maps_residual_conflict_to_tool_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#144-2: two consecutive 409 conflicts surface as ToolError, not a raw SDK error."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": "a",
                    "daimon_account": str(account_id),
                },
            ).model_dump(mode="json"),
        )

    def on_update(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return _conflict_response()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_no_retry_anthropic(router)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="modified concurrently"):
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="a",
            model=None,
            description="updated",
            system=None,
            tools=None,
            mcp_servers=None,
            skills=None,
        )


async def test_attach_mcp_server_retries_once_on_version_conflict(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#144-2: conflict on first attach attempt retries with a fresh agent; result is success."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    retrieve_calls: list[str] = []
    update_calls: list[int] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        retrieve_calls.append("retrieve")
        return httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                mcp_servers=[],
                version=1,
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": "a",
                    "daimon_account": str(account_id),
                },
            ).model_dump(mode="json"),
        )

    def on_update(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(len(update_calls) + 1)
        if len(update_calls) == 1:
            return _conflict_response()
        return httpx.Response(
            200,
            json=ma_agent(id="ag_a", name="a", version=2).model_dump(mode="json"),
        )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_no_retry_anthropic(router)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="a",
        server_name="ctx7",
        url="https://ctx7.example/mcp",
    )
    assert isinstance(result, AgentInfo), "#144-2: attach should return AgentInfo on retry success"
    assert len(update_calls) == 2, "#144-2: must attempt update exactly twice (conflict + retry)"
    assert len(retrieve_calls) == 2, "#144-2: must re-retrieve agent after conflict"


async def test_attach_mcp_server_maps_residual_conflict_to_tool_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#144-2c: two consecutive 409 conflicts on attach surface as ToolError, not a raw SDK error."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                mcp_servers=[],
                version=1,
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": "a",
                    "daimon_account": str(account_id),
                },
            ).model_dump(mode="json"),
        )

    def on_update(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return _conflict_response()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    mcp_servers=[],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_no_retry_anthropic(router)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="modified concurrently"):
        await _attach_mcp_server_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            agent_name="a",
            server_name="ext-mcp",
            url="https://external.example/mcp",
        )


# ---------------------------------------------------------------------------
# Reachability gate: non-admin edits to a currently-reachable (non-seeded) agent
#
# blast radius = one agent -> open; blast radius = the tenant -> admin. A
# stamped (non-seeded) agent nobody has scoped is a member's own scratchpad;
# the moment it is a channel or workspace default, its name-visible
# configuration fields (system/model/skills/mcp_servers/tools) require admin
# at call time. description stays open regardless — it is a label, not a
# spec field.
# ---------------------------------------------------------------------------


def _scoped_agent_router(
    *, tenant_id: uuid.UUID, account_id: uuid.UUID, agent_name: str
) -> tuple[dict[str, Any], AsyncAnthropic]:
    """MARouter + fake client for one stamped (non-system) agent; captures PATCH bodies."""
    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(
            200,
            json=ma_agent(id="ag_scoped", name=agent_name, mcp_servers=[]).model_dump(mode="json"),
        )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_scoped",
                    name=agent_name,
                    mcp_servers=[],
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": agent_name,
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(id="ag_scoped", name=agent_name, mcp_servers=[]).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    return captured, build_fake_anthropic(router.dispatch)


async def _make_tenant_with_default_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    agent_name: str | None,
) -> uuid.UUID:
    """Insert a real tenant; if `agent_name` is given, set it as the workspace default."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord")
        if agent_name is not None:
            await set_fields(
                session,
                scope=TenantScopeRef(tenant_id=tenant.id),
                tenant_id=tenant.id,
                agent_name=agent_name,
                mode="agent",
            )
    return tenant.id


async def test_update_agent_impl_rejects_non_admin_system_patch_when_agent_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    _, client = _scoped_agent_router(
        tenant_id=tenant_id, account_id=account_id, agent_name="scoped-agent"
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    with pytest.raises(ToolError, match="admin must change"):
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="scoped-agent",
            model=None,
            description=None,
            system="new system prompt",
            tools=None,
            mcp_servers=None,
            skills=None,
        )


async def test_update_agent_impl_allows_non_admin_description_patch_when_agent_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    captured, client = _scoped_agent_router(
        tenant_id=tenant_id, account_id=account_id, agent_name="scoped-agent"
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="scoped-agent",
        model=None,
        description="a label, not a spec field",
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    assert captured.get("description") == "a label, not a spec field", (
        "description is not in the gated field set — it must reach MA even on a reachable agent"
    )


async def test_update_agent_impl_rejects_non_admin_skills_patch_when_agent_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    _, client = _scoped_agent_router(
        tenant_id=tenant_id, account_id=account_id, agent_name="scoped-agent"
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    with pytest.raises(ToolError, match="admin must change"):
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="scoped-agent",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=["some-skill"],
        )


async def test_update_agent_impl_rejects_non_admin_mcp_servers_patch_when_agent_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    _, client = _scoped_agent_router(
        tenant_id=tenant_id, account_id=account_id, agent_name="scoped-agent"
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    with pytest.raises(ToolError, match="admin must change"):
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="scoped-agent",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=[{"name": "ext", "type": "url", "url": "https://ext.example/mcp"}],
            skills=None,
        )


async def test_update_agent_impl_rejects_non_admin_tools_patch_when_agent_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    _, client = _scoped_agent_router(
        tenant_id=tenant_id, account_id=account_id, agent_name="scoped-agent"
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    with pytest.raises(ToolError, match="admin must change"):
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="scoped-agent",
            model=None,
            description=None,
            system=None,
            tools=[{"type": "agent_toolset_20260401"}],
            mcp_servers=None,
            skills=None,
        )


async def test_update_agent_impl_allows_non_admin_system_patch_when_agent_unreachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name=None)
    captured, client = _scoped_agent_router(
        tenant_id=tenant_id, account_id=account_id, agent_name="unscoped-agent"
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="unscoped-agent",
        model=None,
        description=None,
        system="new system prompt",
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    assert "new system prompt" in (captured.get("system") or ""), (
        "an agent nobody has scoped is not gated — it's a member's own scratchpad"
    )


async def test_update_agent_impl_allows_admin_system_patch_without_a_reach_read(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin short-circuits the reachability gate entirely — no config read required.

    The tenant has no rows at all; the only read is channel isolation's empty
    access policy.
    """
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    _, client = _scoped_agent_router(
        tenant_id=tenant_id, account_id=account_id, agent_name="scoped-agent"
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="scoped-agent",
        model=None,
        description=None,
        system="new system prompt",
        tools=None,
        mcp_servers=None,
        skills=None,
    )
    assert isinstance(result, AgentInfo), "admin update must succeed without any config read"


async def test_attach_mcp_server_impl_rejects_non_admin_when_agent_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    _, client = _scoped_agent_router(
        tenant_id=tenant_id, account_id=account_id, agent_name="scoped-agent"
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    with pytest.raises(ToolError, match="admin must change"):
        await _attach_mcp_server_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            agent_name="scoped-agent",
            server_name="ctx7",
            url="https://ctx7.example/mcp",
        )


async def test_attach_mcp_server_impl_allows_non_admin_when_agent_unreachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name=None)
    captured, client = _scoped_agent_router(
        tenant_id=tenant_id, account_id=account_id, agent_name="unscoped-agent"
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    await _attach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="unscoped-agent",
        server_name="ctx7",
        url="https://ctx7.example/mcp",
    )
    assert "mcp_servers" in captured, "an unreachable agent's MCP attachment is not gated"


async def test_fork_agent_impl_refuses_a_non_admin_even_for_the_seeded_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Forking is admin-only for every source: a fork routes nowhere and carries
    no pin, so a member could otherwise run a copy anywhere."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_src", name="daimon")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def on_create(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_new", name="my-fork")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="daimon",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    with pytest.raises(ToolError, match="requires a workspace or server admin"):
        await _fork_agent_impl(
            _runtime(client, session_factory=db_session_factory, fernet=fernet),
            auth,
            source_name="daimon",
            new_name="my-fork",
        )


async def test_fork_agent_impl_refuses_to_copy_a_pinned_agent_even_for_an_admin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A copy would be the pinned agent's prompt, skills and connectors with no pin."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"acme-project": ("C_ACME",)}),
    )
    await db_session.commit()
    router = _fork_agent_router(
        tenant_id=tenant.id,
        source_id="ag_acme",
        source_name="acme-project",
        fork_id="ag_copy",
        fork_name="acme-copy",
    )
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant.id, role=Role.ADMIN, is_admin=True
    )

    with pytest.raises(
        ToolError, match="agent rule limiting where it runs|runs it only in certain channels"
    ):
        await _fork_agent_impl(
            _runtime(
                build_fake_anthropic(router.dispatch),
                session_factory=db_session_factory,
                fernet=build_multifernet((Fernet.generate_key().decode(),)),
            ),
            auth,
            source_name="acme-project",
            new_name="acme-copy",
        )


async def test_fork_agent_impl_refuses_a_non_admin_copying_a_project_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fork carries the source's repo binding and tokens and is pinned nowhere, so a
    member copying another client's agent would walk off with its credentials."""
    tenant_id = uuid.uuid4()
    created: list[dict[str, Any]] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        return httpx.Response(200, json=ma_agent(id="ag_new", name="loot").model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_src",
                    name="acme-project",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "acme-project",
                        "daimon_account": str(uuid.uuid4()),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _r, _m: httpx.Response(
            200, json=ma_agent(id="ag_src", name="acme-project").model_dump(mode="json")
        ),
    )
    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    with pytest.raises(ToolError, match="requires a workspace or server admin"):
        await _fork_agent_impl(
            _runtime(client, session_factory=db_session_factory, fernet=fernet),
            auth,
            source_name="acme-project",
            new_name="loot",
        )
    assert created == [], "a refused fork must create nothing"


# ---------------------------------------------------------------------------
# Proactive merged-count cap enforcement (D-16): update_agent must refuse a
# merge that exceeds the shared per-agent cap BEFORE calling agents.update,
# so chat refuses at the same number the /agent-setup panel disables at.
# ---------------------------------------------------------------------------


def _anthropic_skill(skill_id: str) -> dict[str, Any]:
    # "version" is required by the SDK response model (ma_agent below
    # builds a real BetaManagedAgentsAgent) but not by the update-patch
    # BetaManagedAgentsSkillParams shape the extra key is simply ignored there.
    return {"type": "anthropic", "skill_id": skill_id, "version": "1"}


def _url_mcp_server(name: str) -> dict[str, Any]:
    return {"name": name, "type": "url", "url": f"https://{name}.example.com/mcp"}


def _cap_router(
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    existing_skills: list[dict[str, Any]],
    existing_mcp_servers: list[dict[str, Any]],
    update_calls: list[dict[str, Any]],
) -> MARouter:
    """MARouter for an agent already holding `existing_skills` /
    `existing_mcp_servers`. Every POST /v1/agents/{id} (agents.update) that
    reaches the transport is recorded in `update_calls` — the proactive-cap
    tests assert this list stays empty."""

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        update_calls.append(body)
        # The response model requires "version" on every skill entry; the
        # update-patch shape (BetaManagedAgentsSkillParams) does not, so
        # merge_skills_with_ma's MA-preserved extras omit it. Backfill for
        # the response only — the assertions below inspect `update_calls`
        # (the raw request body), not this response.
        response_skills = [{**s, "version": s.get("version", "1")} for s in body.get("skills", [])]
        return httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                skills=response_skills or existing_skills,
                mcp_servers=body.get("mcp_servers", existing_mcp_servers),
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="a",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "a",
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(
            200,
            json=ma_agent(
                id="ag_a",
                name="a",
                skills=existing_skills,
                mcp_servers=existing_mcp_servers,
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    return router


async def test_update_agent_refuses_when_merged_skills_exceed_the_product_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    existing_skills = [_anthropic_skill(f"existing-{i}") for i in range(AGENT_SKILL_CAP - 1)]
    new_skills = [_anthropic_skill("new-a"), _anthropic_skill("new-b")]
    update_calls: list[dict[str, Any]] = []
    router = _cap_router(
        tenant_id=tenant_id,
        account_id=account_id,
        existing_skills=existing_skills,
        existing_mcp_servers=[],
        update_calls=update_calls,
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    with pytest.raises(ToolError) as exc_info:
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="a",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=new_skills,  # type: ignore[arg-type]
        )
    assert str(AGENT_SKILL_CAP) in str(exc_info.value), (
        f"refusal must name the cap; got {exc_info.value}"
    )
    assert "remove_skill" in str(exc_info.value) and "No skills were changed" in str(
        exc_info.value
    ), "cap refusal must name the removal path and failed-save outcome"
    assert not update_calls, "the merged-count refusal must fire before any agents.update request"


async def test_update_agent_allows_a_skill_merge_exactly_at_the_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    existing_skills = [_anthropic_skill(f"existing-{i}") for i in range(AGENT_SKILL_CAP - 1)]
    new_skills = [_anthropic_skill("new-a")]
    update_calls: list[dict[str, Any]] = []
    router = _cap_router(
        tenant_id=tenant_id,
        account_id=account_id,
        existing_skills=existing_skills,
        existing_mcp_servers=[],
        update_calls=update_calls,
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    result = await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=new_skills,  # type: ignore[arg-type]
    )
    assert isinstance(result, AgentInfo), "a merge landing exactly at the cap must succeed"
    assert len(update_calls) == 1, "the at-cap merge must reach agents.update exactly once"
    assert len(update_calls[0]["skills"]) == AGENT_SKILL_CAP, (
        "the merged patch must carry exactly AGENT_SKILL_CAP skills"
    )


async def test_update_agent_refuses_when_merged_mcp_servers_exceed_the_product_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    existing_mcp_servers = [_url_mcp_server(f"existing-{i}") for i in range(AGENT_MCP_CAP - 1)]
    new_mcp_servers = [_url_mcp_server("new-a"), _url_mcp_server("new-b")]
    update_calls: list[dict[str, Any]] = []
    router = _cap_router(
        tenant_id=tenant_id,
        account_id=account_id,
        existing_skills=[],
        existing_mcp_servers=existing_mcp_servers,
        update_calls=update_calls,
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    with pytest.raises(ToolError) as exc_info:
        await _update_agent_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            name="a",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=new_mcp_servers,  # type: ignore[arg-type]
            skills=None,
        )
    assert str(AGENT_MCP_CAP) in str(exc_info.value), (
        f"refusal must name the cap; got {exc_info.value}"
    )
    assert not update_calls, "the merged-count refusal must fire before any agents.update request"


async def test_update_agent_allows_an_mcp_merge_exactly_at_the_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    existing_mcp_servers = [_url_mcp_server(f"existing-{i}") for i in range(AGENT_MCP_CAP - 1)]
    new_mcp_servers = [_url_mcp_server("new-a")]
    update_calls: list[dict[str, Any]] = []
    router = _cap_router(
        tenant_id=tenant_id,
        account_id=account_id,
        existing_skills=[],
        existing_mcp_servers=existing_mcp_servers,
        update_calls=update_calls,
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    result = await _update_agent_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=new_mcp_servers,  # type: ignore[arg-type]
        skills=None,
    )
    assert isinstance(result, AgentInfo), "a merge landing exactly at the cap must succeed"
    assert len(update_calls) == 1, "the at-cap merge must reach agents.update exactly once"
    assert len(update_calls[0]["mcp_servers"]) == AGENT_MCP_CAP, (
        "the merged patch must carry exactly AGENT_MCP_CAP mcp servers"
    )


def test_create_spec_rejects_a_model_this_deployment_does_not_meter() -> None:
    """The chat path validates model ids the way the panel always has.

    An unmetered id is worse than a wrong one: the agent is created, fails only
    when a session tries to start, and every turn it does run is billed by
    Anthropic while `pricing.cost_of` returns None for it — spend that never
    reaches the ledger. `claude-opus-4-5` is real and in the SDK's Literal, but
    absent from AGENT_MODEL_PRICING, which is exactly the gap.
    """
    from daimon.adapters.mcp.tools.agents import (
        _build_create_spec,  # pyright: ignore[reportPrivateUsage]
    )

    with pytest.raises(ToolError) as excinfo:
        _build_create_spec(
            name="a",
            model="claude-opus-4-5",
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skill_repos=None,
        )
    assert "claude-opus-5" in str(excinfo.value), (
        "the refusal must list what IS available, or the caller has to guess"
    )


def test_create_spec_accepts_the_current_generation_models() -> None:
    from daimon.adapters.mcp.tools.agents import (
        _build_create_spec,  # pyright: ignore[reportPrivateUsage]
    )

    for model in ("claude-sonnet-5-5", "claude-opus-5-5"):
        spec = _build_create_spec(
            name="a",
            model=model,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skill_repos=None,
        )
        assert spec.model == model, f"{model} must be accepted"


# ---------------------------------------------------------------------------
# A newly created agent that nothing routes to says so — `AgentInfo.answering`
# ---------------------------------------------------------------------------


def _create_demo_router() -> AsyncAnthropic:
    """Serve the empty name-collision listing plus create/retrieve for one agent."""
    body = ma_agent(id="ag_new", name="demo").model_dump(mode="json")
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    router.add("POST", r"/v1/agents", lambda _req, _m: httpx.Response(200, json=body))
    router.add("GET", r"/v1/agents/([^/]+)", lambda _req, _m: httpx.Response(200, json=body))
    return build_fake_anthropic(router.dispatch)


async def test_create_agent_returns_unrouted_note_when_not_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name=None)
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )

    result = await _create_agent_impl(
        _runtime(_create_demo_router(), session_factory=db_session_factory),
        auth,
        AgentSpec(name="demo", model="claude-opus-4-5"),
    )

    assert result.answering is not None, (
        "a new agent nothing routes to must come back with the routing handoff"
    )
    assert UNROUTED_LINE in result.answering, (
        "the handoff must be the shared unrouted copy, not a second wording"
    )


async def test_create_agent_returns_no_unrouted_note_when_a_config_row_names_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="demo")
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )

    result = await _create_agent_impl(
        _runtime(_create_demo_router(), session_factory=db_session_factory),
        auth,
        AgentSpec(name="demo", model="claude-opus-4-5"),
    )

    assert result.answering is None, (
        "an agent the workspace default already names is reachable — nothing to hand off"
    )


async def test_fork_agent_returns_unrouted_note_when_not_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name=None)
    source = ma_agent(
        id="ag_src",
        name="source",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "source"},
    )
    router = MARouter()
    router.add_agent_list(source)
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=source.model_dump(mode="json")),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda _req, _m: httpx.Response(
            200, json=ma_agent(id="ag_new", name="myfork").model_dump(mode="json")
        ),
    )
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )

    result = await _fork_agent_impl(
        _runtime(
            build_fake_anthropic(router.dispatch),
            session_factory=db_session_factory,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
        ),
        auth,
        source_name="source",
        new_name="myfork",
    )

    assert result.answering is not None, (
        "a fork nothing routes to must come back with the routing handoff"
    )
    assert UNROUTED_LINE in result.answering, "the fork handoff must reuse the shared unrouted copy"


@pytest.mark.parametrize(
    ("server", "public_url"),
    [
        # Re-pointing the reserved name anywhere else.
        ({"name": "daimon-mcp", "type": "url", "url": "https://evil.example/mcp"}, None),
        (
            {"name": "daimon-mcp", "type": "url", "url": "https://evil.example/mcp"},
            "https://mcp.example.com/mcp",
        ),
        # The deployment's own endpoint under another name.
        (
            {"name": "shadow", "type": "url", "url": "https://mcp.example.com/mcp"},
            "https://mcp.example.com/mcp",
        ),
    ],
)
async def test_update_agent_refuses_to_repoint_the_reserved_server(
    server: dict[str, str], public_url: str | None
) -> None:
    """`update_agent` merges servers by name, so it gets the same reserved-name
    guard as `attach_mcp_server` — the tool-safety exemption relies on it."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    update_calls: list[dict[str, Any]] = []
    router = _cap_router(
        tenant_id=tenant_id,
        account_id=account_id,
        existing_skills=[],
        existing_mcp_servers=[],
        update_calls=update_calls,
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    with pytest.raises(ToolError, match="reserved|deployment's own"):
        await _update_agent_impl(
            _runtime(client, public_url=public_url),
            auth,
            name="a",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=[server],  # type: ignore[list-item]
            skills=None,
        )
    assert not update_calls


async def test_update_agent_accepts_the_canonical_reserved_entry_round_trip(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    update_calls: list[dict[str, Any]] = []
    router = _cap_router(
        tenant_id=tenant_id,
        account_id=account_id,
        existing_skills=[],
        existing_mcp_servers=[],
        update_calls=update_calls,
    )
    client = build_fake_anthropic(router.dispatch)
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    await _update_agent_impl(
        _runtime(
            client, session_factory=db_session_factory, public_url="https://mcp.example.com/mcp"
        ),
        auth,
        name="a",
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": "https://mcp.example.com/mcp/"}],
        skills=None,
    )
    assert len(update_calls) == 1


def _personal_agent_router(
    *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> tuple[list[dict[str, Any]], AsyncAnthropic]:
    """One agent that already has `ctx7` at the real URL; captures update bodies."""
    updates: list[dict[str, Any]] = []
    body = ma_agent(
        id="ag_personal",
        name="personal-bot",
        mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://real.example/mcp"}],
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "personal-bot",
            "daimon_account": str(account_id),
        },
    ).model_dump(mode="json")

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        updates.append(json_body(req))
        return httpx.Response(200, json=body)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([body]))
    router.add("GET", r"/v1/agents/([^/]+)", lambda _req, _m: httpx.Response(200, json=body))
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    return updates, build_fake_anthropic(router.dispatch)


@pytest.mark.parametrize("tool", ["attach_mcp_server", "update_agent"])
async def test_member_cannot_repoint_a_server_on_someones_personal_default_agent(
    db_session_factory: async_sessionmaker[AsyncSession], tool: str
) -> None:
    """H2: a personal default answers another person, so repointing its server is
    `mcp_replace` and needs an admin, though the plain reachability gate passes."""
    from daimon.core.scope import UserScopeRef
    from daimon.testing.factories import make_account

    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name=None)
    async with db_session_factory() as session, session.begin():
        from daimon.core.stores.tenants import get_tenant

        tenant = await get_tenant(session, tenant_id)
        owner = await make_account(session, tenant=tenant)
        await set_fields(
            session,
            scope=UserScopeRef(account_id=owner.id),
            tenant_id=tenant_id,
            agent_name="personal-bot",
        )
    member = uuid.uuid4()
    updates, client = _personal_agent_router(tenant_id=tenant_id, account_id=member)
    auth = AuthIdentity(account_id=member, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    runtime = _runtime(client, session_factory=db_session_factory)

    with pytest.raises(ToolError, match="admin"):
        if tool == "attach_mcp_server":
            await _attach_mcp_server_impl(
                runtime,
                auth,
                agent_name="personal-bot",
                server_name="ctx7",
                url="https://attacker.example/mcp",
            )
        else:
            await _update_agent_impl(
                runtime,
                auth,
                name="personal-bot",
                model=None,
                description=None,
                system=None,
                tools=None,
                mcp_servers=[
                    {"name": "ctx7", "type": "url", "url": "https://attacker.example/mcp"}
                ],
                skills=None,
            )
    assert updates == [], "the server must not be repointed"
