"""DB-backed tests for the channel skill MCP tools: server admins only, audited."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _channel_target as channel_target
from daimon.adapters.mcp.tools.channel_skills import (
    _add,  # pyright: ignore[reportPrivateUsage]
    _list,  # pyright: ignore[reportPrivateUsage]
    _remove,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.scope import DeploymentDefault
from daimon.core.security_audit import capture_decision
from daimon.core.stores import channel_skills
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role, TenantRow
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic
from daimon.testing.ma_models import ma_agent
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CHANNEL = "222"
_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC).isoformat()


@pytest.fixture(autouse=True)
def visible(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
        return channel_id

    monkeypatch.setattr(channel_target, "resolve_visible_channel", fake)


def _runtime(sessionmaker: async_sessionmaker[AsyncSession], tenant: TenantRow) -> McpRuntime:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/skills":
            title = tenant_scoped_display_title(tenant_id=tenant.id, name="pdf-tools")
            skill = {
                "id": "skill_lib",
                "created_at": _NOW,
                "display_title": title,
                "latest_version": "v3",
                "source": "custom",
                "type": "skill",
                "updated_at": _NOW,
            }
            return httpx.Response(200, json={"data": [skill], "next_page": None})
        assert request.url.path == "/v1/agents"
        agent = ma_agent(name="shared", tenant_id=tenant.id).model_dump(mode="json")
        return httpx.Response(200, json={"data": [agent], "next_page": None})

    return McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(handler),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        deployment_default=DeploymentDefault(agent_name="shared"),
    )


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[TenantRow, uuid.UUID]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        return tenant, (await make_account(session, tenant=tenant)).id


def _auth(tenant: TenantRow, account_id: uuid.UUID, *, admin: bool) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant.id,
        role=Role.ADMIN if admin else Role.USER,
        platform="discord",
        is_admin=admin,
    )


async def test_a_server_admin_adds_lists_and_removes_a_channel_skill(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant)
    admin = _auth(tenant, account_id, admin=True)

    with capture_decision() as allowed:
        added = await _add(runtime, admin, _CHANNEL, "pdf-tools")
    assert (allowed.operation, allowed.denied) == ("set_channel_skills", False)
    assert [(s.skill_id, s.version, s.agent_name) for s in added.skills] == [
        ("skill_lib", "v3", None)
    ]
    assert [s.name for s in (await _list(runtime, admin, _CHANNEL)).skills] == ["pdf-tools"]

    removed = await _remove(runtime, admin, _CHANNEL, "pdf-tools")
    assert removed.skills == []
    with pytest.raises(ToolError, match="doesn't add that skill"):
        await _remove(runtime, admin, _CHANNEL, "pdf-tools")


async def test_a_channel_admin_cannot_add_or_remove_even_their_own_channels_skills(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant)
    channel_admin = dataclasses.replace(
        _auth(tenant, account_id, admin=False),
        platform_user_id="u-ca",
        administered_channel_ids=frozenset({_CHANNEL}),
    )

    for change in (_add, _remove):
        with capture_decision() as denied, pytest.raises(ToolError, match="needs a workspace"):
            await change(runtime, channel_admin, _CHANNEL, "pdf-tools")
        assert (denied.operation, denied.reason) == (
            "set_channel_skills",
            "authz:admin_required",
        ), "the refusal is audited"
    with pytest.raises(ToolError):
        await _list(runtime, channel_admin, _CHANNEL)
    async with committing_sessionmaker() as session:
        rows = await channel_skills.list_channel_skills(
            session, tenant_id=tenant.id, platform="discord"
        )
    assert rows == []


async def test_an_isolated_agents_upload_is_left_out_of_a_listing_from_outside(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant)
    async with committing_sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                sealed_channel_ids=(_CHANNEL,),
                isolated_channel_ids=(_CHANNEL,),
                agent_channel_pins={"rx": (_CHANNEL,)},
            ),
        )
        for skill_id, owner in (("skill_rx", "rx"), ("skill_lib", None)):
            await channel_skills.add_channel_skill(
                session,
                tenant_id=tenant.id,
                platform="discord",
                channel_id=_CHANNEL,
                skill_id=skill_id,
                version="v1",
                name=skill_id,
                owner_agent_name=owner,
                actor_account_id=None,
            )

    listed = await _list(runtime, _auth(tenant, account_id, admin=True), _CHANNEL)

    assert [s.skill_id for s in listed.skills] == ["skill_lib"]
    assert "rx" not in listed.summary
