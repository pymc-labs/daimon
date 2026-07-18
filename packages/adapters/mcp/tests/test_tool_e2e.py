"""End-to-end: FastMCP in-process Client -> middleware -> tool -> MA transport fake.

Uses validated construction for every MA response via a transport-level
httpx.MockTransport rather than DB store seeding for agents/environments.
Identity is injected via custom subject_resolver + tenant_resolver.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp.middleware.mcp_identity import ClaimResolver
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core._models import Account
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware import MiddlewareContext
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .factories import make_ma_agent, seed_tenant_and_account

pytestmark = pytest.mark.asyncio


def _fixed_resolvers(
    account_id: uuid.UUID, tenant_id: uuid.UUID, role: str = "admin"
) -> tuple[ClaimResolver, ClaimResolver, ClaimResolver]:
    async def subject_resolver(_ctx: MiddlewareContext) -> str:
        return str(account_id)

    async def tenant_resolver(_ctx: MiddlewareContext) -> str:
        return str(tenant_id)

    async def role_resolver(_ctx: MiddlewareContext) -> str:
        return role

    return subject_resolver, tenant_resolver, role_resolver


async def _admin_is_admin_resolver(_ctx: MiddlewareContext) -> str | None:
    """is_admin resolver for mutating-tool e2e tests.

    The tokenless StaticTokenVerifier harness has no is_admin claim, so the
    default production_is_admin_resolver yields is_admin=False — which makes the
    The _require_admin gate refuses mutating tools. Tests that
    exercise a mutating tool's happy path pass this to run as an admin caller.
    Read-only tools are ungated and do not need it.
    """
    return "true"


async def test_list_agents_end_to_end(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await seed_tenant_and_account(db_session)
    await db_session.commit()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                make_ma_agent(
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    fake_client = build_fake_anthropic(router.dispatch)

    sub_resolver, tid_resolver, role_resolver = _fixed_resolvers(account_id, tenant_id)
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(public_url=HttpUrl("https://t.example.com/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        subject_resolver=sub_resolver,
        tenant_resolver=tid_resolver,
        role_resolver=role_resolver,
        anthropic=fake_client,
    )
    mcp = app.state.mcp  # type: ignore[attr-defined]

    async with Client(mcp) as client:
        result = await client.call_tool("list_agents", {})
        agents = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert [a["name"] for a in agents] == ["demo"], "should list tenant's agent"


async def test_create_agent_end_to_end_validates_ma_response(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await seed_tenant_and_account(db_session)
    await db_session.commit()

    ma_response = make_ma_agent(id="ag_new", name="newagent")

    def handler(request: httpx.Request) -> httpx.Response:
        # reconcile calls GET /agents (list for collision + dedup), POST /agents (create),
        # then GET /agents/{id} (re-retrieve for _build_agent_info).
        return httpx.Response(200, json=ma_response.model_dump(mode="json"))

    sub_resolver, tid_resolver, role_resolver = _fixed_resolvers(account_id, tenant_id)
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(public_url=HttpUrl("https://t.example.com/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        subject_resolver=sub_resolver,
        tenant_resolver=tid_resolver,
        role_resolver=role_resolver,
        is_admin_resolver=_admin_is_admin_resolver,
        anthropic=build_fake_anthropic(handler),
    )
    mcp = app.state.mcp  # type: ignore[attr-defined]

    async with Client(mcp) as client:
        result = await client.call_tool(
            "create_agent",
            {"name": "newagent", "model": "claude-opus-4-5"},
        )
        row = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert row["name"] == "newagent", "should persist created agent name"
        assert row["id"] == "ag_new", "should store MA-assigned id"


async def test_list_agents_isolates_tenants_end_to_end(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Two tenants — each sees only their own agents."""
    tenant_a_id, account_a_id = await seed_tenant_and_account(db_session)
    tenant_b = await make_tenant(db_session, platform="discord", workspace_id="guild-e2e-b")
    account_b = Account(tenant_id=tenant_b.id)
    db_session.add(account_b)
    await db_session.flush()
    tenant_b_id = tenant_b.id
    account_b_id = account_b.id
    await db_session.commit()

    # Both apps share a transport that returns all agents; each app filters to its tenant
    all_agents = [
        make_ma_agent(
            id="ag_a",
            name="a-agent",
            metadata={"daimon_tenant": str(tenant_a_id), "daimon_name": "a-agent"},
        ).model_dump(mode="json"),
        make_ma_agent(
            id="ag_b",
            name="b-agent",
            metadata={"daimon_tenant": str(tenant_b_id), "daimon_name": "b-agent"},
        ).model_dump(mode="json"),
    ]

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response(all_agents))
    shared_client = build_fake_anthropic(router.dispatch)

    _auth_stub = StaticTokenVerifier(tokens={})

    sub_a, tid_a, role_a = _fixed_resolvers(account_a_id, tenant_a_id)
    app_a = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(public_url=HttpUrl("https://t.example.com/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=_auth_stub,
        subject_resolver=sub_a,
        tenant_resolver=tid_a,
        role_resolver=role_a,
        anthropic=shared_client,
    )
    sub_b, tid_b, role_b = _fixed_resolvers(account_b_id, tenant_b_id)
    app_b = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(public_url=HttpUrl("https://t.example.com/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=_auth_stub,
        subject_resolver=sub_b,
        tenant_resolver=tid_b,
        role_resolver=role_b,
        anthropic=shared_client,
    )
    mcp_a = app_a.state.mcp  # type: ignore[attr-defined]
    mcp_b = app_b.state.mcp  # type: ignore[attr-defined]

    async with Client(mcp_a) as client_a:
        page_a = json.loads((await client_a.call_tool("list_agents", {})).content[0].text)  # type: ignore[union-attr]

    async with Client(mcp_b) as client_b:
        page_b = json.loads((await client_b.call_tool("list_agents", {})).content[0].text)  # type: ignore[union-attr]

    assert [a["name"] for a in page_a] == ["a-agent"], "tenant A sees only its agent"
    assert [a["name"] for a in page_b] == ["b-agent"], "tenant B sees only its agent"


async def test_list_skills_end_to_end(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await seed_tenant_and_account(db_session)
    await db_session.commit()

    # Use canonical prefixed display_title so the tenant-scoped list includes this skill.
    e2e_skill_title = f"{str(tenant_id)[:8]}-e2e-skill"
    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_e2e",
                    display_title=e2e_skill_title,
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json")
            ]
        ),
    )
    fake_client = build_fake_anthropic(router.dispatch)

    sub_resolver, tid_resolver, role_resolver = _fixed_resolvers(account_id, tenant_id)
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(public_url=HttpUrl("https://t.example.com/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        subject_resolver=sub_resolver,
        tenant_resolver=tid_resolver,
        role_resolver=role_resolver,
        anthropic=fake_client,
    )
    mcp = app.state.mcp  # type: ignore[attr-defined]

    async with Client(mcp) as client:
        result = await client.call_tool("skills_list", {})
        skills = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert [s["name"] for s in skills] == ["e2e-skill"], (
            "should list the e2e skill with bare name"
        )
