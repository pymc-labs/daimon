"""An agent's owner manages that agent's GitHub repos from chat; nobody else gains anything."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import github_connect as connect_tool
from daimon.adapters.mcp.tools import github_remove_repo as remove_tool
from daimon.adapters.mcp.tools import github_working_repo as working_tool
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    GithubAppSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners, github_access, github_app_installations
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import Role
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_tenant_github_repo,
    mark_principal_foreign,
)
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

INSTALLATION = 77
REPOS = {101: "team-a/app", 102: "team-a/docs", 103: "team-a/shared"}
MA_AGENT = "ma_agent"
MA_OTHER = "ma_other"


class World:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
        self.sessions = sessions
        self.tenant_id = tenant_id
        self.accounts: dict[str, uuid.UUID] = {}

    def agent(self, ma_agent_id: str = MA_AGENT) -> uuid.UUID:
        return derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=ma_agent_id)

    def auth(self, who: str, platform: str = "discord") -> AuthIdentity:
        return AuthIdentity(
            account_id=self.accounts[who],
            tenant_id=self.tenant_id,
            role=Role.ADMIN if who == "admin" else Role.USER,
            platform=platform,
            external_id="workspace",
            platform_user_id=who,
            is_admin=who == "admin",
        )


@pytest.fixture
async def world(committing_sessionmaker: async_sessionmaker[AsyncSession]) -> World:
    """An admin, a primary owner, a co-owner, a foreign co-owner and a member of one agent."""
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"owners-{uuid.uuid4().hex[:8]}")
        built = World(committing_sessionmaker, tenant.id)
        for who in ("admin", "owner", "co_owner", "foreign_co_owner", "member"):
            account = await make_account(session, tenant=tenant)
            await make_platform_principal(
                session, tenant=tenant, account=account, platform="discord", external_id=who
            )
            if who == "admin":
                await set_role(session, account.id, Role.ADMIN)
            built.accounts[who] = account.id
        await agent_owners.set_owner(
            session,
            tenant_id=tenant.id,
            agent_id=built.agent(),
            owner_account_id=built.accounts["owner"],
            source="admin_assigned",
        )
        for who in ("co_owner", "foreign_co_owner"):
            await agent_owners.add_co_owner(
                session,
                tenant_id=tenant.id,
                agent_id=built.agent(),
                account_id=built.accounts[who],
                added_by_account_id=built.accounts["admin"],
            )
        await mark_principal_foreign(session, account_id=built.accounts["foreign_co_owner"])
        await github_app_installations.upsert(
            session,
            installation_id=INSTALLATION,
            account_login="team-a",
            repo_full_names=list(REPOS.values()),
        )
    return built


def _runtime(sessions: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return McpRuntime(
        session_factory=sessions,
        client=AsyncMock(),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
            mcp=McpSettings(public_url=HttpUrl("https://mcp.test/mcp")),
            github_app=GithubAppSettings(
                app_id="42",
                app_slug="sample-app",
                private_key=SecretStr("pem"),
                client_id="client",
                client_secret=SecretStr("secret"),
            ),
        ),
        deployment_default=DeploymentDefault(),
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
    )


def _patch_chat(monkeypatch: pytest.MonkeyPatch, module: object, ma_agent_id: str = MA_AGENT):
    origin = SimpleNamespace(
        id=uuid.uuid4(),
        configuration_target_name="Agent",
        configuration_target_ma_agent_id=ma_agent_id,
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="channel",
        thread_id="thread",
        sender_foreign=False,
    )
    agent = SimpleNamespace(id=ma_agent_id, name="Agent", metadata={})
    monkeypatch.setattr(module, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(module, "resolve_setup_agent", AsyncMock(return_value=agent))


async def _connect_repo(
    world: World,
    repo_id: int,
    *,
    ma_agent_id: str = MA_AGENT,
    server_wide: bool = False,
    working: bool = False,
) -> None:
    """A repo connected (for the agent only, or server-wide) and granted to the agent."""
    agent_id = world.agent(ma_agent_id)
    async with world.sessions.begin() as session:
        if not await _repo_connected(session, world, repo_id):
            await make_tenant_github_repo(
                session,
                tenant_id=world.tenant_id,
                repo_id=repo_id,
                full_name=REPOS[repo_id],
                installation_id=INSTALLATION,
                scope_agent_id=None if server_wide else agent_id,
                authorized_by_account_id=world.accounts["admin"],
            )
        await github_access.stage_grant(
            session,
            tenant_id=world.tenant_id,
            agent_id=agent_id,
            repo_id=repo_id,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=world.accounts["admin"],
            is_working_repo=working,
        )


async def _repo_connected(session: AsyncSession, world: World, repo_id: int) -> bool:
    count = await session.scalar(
        text("SELECT count(*) FROM tenant_github_repos WHERE tenant_id = :t AND repo_id = :r"),
        {"t": world.tenant_id, "r": repo_id},
    )
    return bool(count)


async def _granted(world: World, ma_agent_id: str = MA_AGENT) -> set[int]:
    async with world.sessions() as session:
        return {
            grant.repo_id
            for grant in await github_access.list_agent_grants(
                session, tenant_id=world.tenant_id, agent_id=world.agent(ma_agent_id)
            )
        }


# ---- set the working repo -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "co_owner"])
async def test_an_owner_sets_the_working_repo_of_their_own_agent(
    world: World, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    await _connect_repo(world, 101)
    _patch_chat(monkeypatch, working_tool)
    result = await working_tool.set_working_repo_impl(
        _runtime(world.sessions),
        world.auth(who),
        origin_context_id=str(uuid.uuid4()),
        repo_name="team-a/app",
    )
    assert result.working_repo == "team-a/app"
    async with world.sessions() as session:
        [grant] = await github_access.list_agent_grants(
            session, tenant_id=world.tenant_id, agent_id=world.agent()
        )
    assert grant.is_working_repo


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["member", "foreign_co_owner"])
async def test_a_member_or_a_foreign_co_owner_cannot_set_the_working_repo(
    world: World, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    await _connect_repo(world, 101)
    _patch_chat(monkeypatch, working_tool)
    with pytest.raises(ToolError, match="Ask a server admin or an owner of Agent"):
        await working_tool.set_working_repo_impl(
            _runtime(world.sessions),
            world.auth(who),
            origin_context_id=str(uuid.uuid4()),
            repo_name="team-a/app",
        )


@pytest.mark.asyncio
async def test_an_owner_cannot_set_the_working_repo_of_an_agent_they_do_not_own(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _connect_repo(world, 101, ma_agent_id=MA_OTHER)
    _patch_chat(monkeypatch, working_tool, MA_OTHER)
    with pytest.raises(ToolError, match="Ask a server admin or an owner of Agent"):
        await working_tool.set_working_repo_impl(
            _runtime(world.sessions),
            world.auth("owner"),
            origin_context_id=str(uuid.uuid4()),
            repo_name="team-a/app",
        )


# ---- remove a repo --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "co_owner"])
async def test_an_owner_removes_a_repo_the_admin_connected_for_their_agent(
    world: World, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    await _connect_repo(world, 101)
    _patch_chat(monkeypatch, remove_tool)
    result = await remove_tool.remove_repo_impl(
        _runtime(world.sessions),
        world.auth(who),
        origin_context_id=str(uuid.uuid4()),
        repo_name="team-a/app",
    )
    assert result.status == "removed"
    assert await _granted(world) == set()


@pytest.mark.asyncio
async def test_removing_a_server_wide_repo_keeps_it_for_the_other_agents(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _connect_repo(world, 103, server_wide=True)
    await _connect_repo(world, 103, ma_agent_id=MA_OTHER, server_wide=True)
    _patch_chat(monkeypatch, remove_tool)
    await remove_tool.remove_repo_impl(
        _runtime(world.sessions),
        world.auth("owner"),
        origin_context_id=str(uuid.uuid4()),
        repo_name="team-a/shared",
    )
    assert await _granted(world) == set()
    assert await _granted(world, MA_OTHER) == {103}
    async with world.sessions() as session:
        row = (
            await session.execute(
                text("SELECT status, scope_agent_id FROM tenant_github_repos WHERE repo_id = 103")
            )
        ).one()
    assert (row.status, row.scope_agent_id) == ("active", None)


@pytest.mark.asyncio
async def test_an_owner_cannot_remove_a_repo_from_an_agent_they_do_not_own(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _connect_repo(world, 101, ma_agent_id=MA_OTHER)
    _patch_chat(monkeypatch, remove_tool, MA_OTHER)
    with pytest.raises(ToolError, match="Ask a server admin or an owner of Agent"):
        await remove_tool.remove_repo_impl(
            _runtime(world.sessions),
            world.auth("owner"),
            origin_context_id=str(uuid.uuid4()),
            repo_name="team-a/app",
        )
    assert await _granted(world, MA_OTHER) == {101}


@pytest.mark.asyncio
async def test_a_member_cannot_remove_a_repo(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await _connect_repo(world, 101)
    _patch_chat(monkeypatch, remove_tool)
    with pytest.raises(ToolError, match="Ask a server admin or an owner of Agent"):
        await remove_tool.remove_repo_impl(
            _runtime(world.sessions),
            world.auth("member"),
            origin_context_id=str(uuid.uuid4()),
            repo_name="team-a/app",
        )
    assert await _granted(world) == {101}


# ---- connect a repo -------------------------------------------------------


async def _connect_from_chat(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    who: str,
    platform: str,
    ma_agent_id: str = MA_AGENT,
):
    _patch_chat(monkeypatch, connect_tool, ma_agent_id)
    monkeypatch.setattr(connect_tool, "_post_slack_connect_card", AsyncMock())
    monkeypatch.setattr(connect_tool, "_post_discord_connect_card", AsyncMock())
    return await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        _runtime(world.sessions),
        world.auth(who, platform),
        origin_context_id=str(uuid.uuid4()),
    )


async def _role(world: World, who: str) -> Role:
    async with world.sessions() as session:
        account = await get_account(session, world.accounts[who])
    assert account is not None
    return account.role


async def _count(world: World, table: str, **where: object) -> int:
    clause = " AND ".join(f"{column} = :{column}" for column in where) or "true"
    async with world.sessions() as session:
        total = await session.scalar(
            text(f"SELECT count(*) FROM {table} WHERE {clause}"),
            where,
        )
    return int(total or 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "discord"])
@pytest.mark.parametrize("who", ["owner", "co_owner"])
async def test_an_owner_gets_the_connect_link_for_their_own_agent_and_is_not_promoted(
    world: World, monkeypatch: pytest.MonkeyPatch, who: str, platform: str
) -> None:
    sent = await _connect_from_chat(world, monkeypatch, who, platform)
    assert sent.status == "sent"
    if platform == "slack":
        assert (
            await _count(
                world,
                "github_connect_invitations",
                requester_account_id=world.accounts[who],
                agent_id=world.agent(),
            )
            == 1
        )
    assert await _role(world, who) == Role.USER, "an owner's Connect never promotes them"


@pytest.mark.asyncio
async def test_an_owner_is_refused_for_an_agent_they_do_not_own(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = await _connect_from_chat(world, monkeypatch, "owner", "slack", MA_OTHER)
    assert refused.status == "ask_admin"
    assert await _count(world, "github_connect_invitations") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["member", "foreign_co_owner"])
async def test_a_member_still_gets_ask_admin_and_a_recorded_request(
    world: World, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    refused = await _connect_from_chat(world, monkeypatch, who, "discord")
    assert refused.status == "ask_admin"
    assert "admin or an owner of Agent" in refused.message
    assert (
        await _count(world, "github_connect_requests", requester_account_id=world.accounts[who])
        == 1
    )
    assert await _count(world, "github_connect_invitations") == 0
    assert await _role(world, who) == Role.USER


@pytest.mark.asyncio
async def test_an_owner_agent_scoped_grant_is_the_only_direct_grant(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo already connected for the agent is granted at once; a server-wide one is not."""
    async with world.sessions.begin() as session:
        for repo_id, scope in ((102, world.agent()), (103, None)):
            await make_tenant_github_repo(
                session,
                tenant_id=world.tenant_id,
                repo_id=repo_id,
                full_name=REPOS[repo_id],
                installation_id=INSTALLATION,
                scope_agent_id=scope,
                authorized_by_account_id=world.accounts["admin"],
            )
    _patch_chat(monkeypatch, connect_tool)
    monkeypatch.setattr(connect_tool, "_post_discord_connect_card", AsyncMock())
    runtime = _runtime(world.sessions)
    granted = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        world.auth("owner"),
        origin_context_id=str(uuid.uuid4()),
        repo_name="team-a/docs",
    )
    assert granted.status == "granted"
    assert await _granted(world) == {102}
    server_wide = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        world.auth("owner"),
        origin_context_id=str(uuid.uuid4()),
        repo_name="team-a/shared",
    )
    assert server_wide.status == "sent", "a server-wide repo goes through the Connect page"
    assert await _granted(world) == {102}
