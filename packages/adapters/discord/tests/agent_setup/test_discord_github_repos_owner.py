"""The agent repos panel admits an owner of that agent, read live, and promotes nobody."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.discord.agent_setup import github_repos as repos_module
from daimon.adapters.discord.agent_setup.github_repos import (
    GitHubReposView,
    mint_agent_connect_url,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.core.github_panel import GrantsPanel
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.native_admins import AdminCaller
from daimon.core.roster import RosterAgent
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = 9100
_AGENT = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="m", is_built_in=False)


@pytest.fixture
async def world(db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]):
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD))
    assert tenant.id == derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD))
    accounts: dict[int, uuid.UUID] = {}
    for user in (1, 2, 3):  # owner, co-owner, member
        account = await make_account(db_session, tenant=tenant)
        await make_platform_principal(
            db_session, tenant=tenant, account=account, platform="discord", external_id=str(user)
        )
        accounts[user] = account.id
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT.ma_agent_id)
    await agent_owners.set_owner(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        owner_account_id=accounts[1],
        source="admin_assigned",
    )
    await agent_owners.add_co_owner(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=accounts[2],
        added_by_account_id=None,
    )
    await db_session.commit()
    runtime = SimpleNamespace(
        anthropic=object(),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        settings=SimpleNamespace(
            mcp=SimpleNamespace(app_root_url="https://mcp.test"),
            github_app=SimpleNamespace(
                app_id="1",
                app_slug="app",
                private_key="key",
                client_id="client",
                client_secret="secret",
            ),
            crypto=SimpleNamespace(keys=[SecretStr(Fernet.generate_key().decode())]),
        ),
    )
    return tenant.id, accounts, runtime


def _click(monkeypatch: pytest.MonkeyPatch, user: int, *, admin: bool = False) -> MagicMock:
    monkeypatch.setattr(
        repos_module,
        "find_agent_by_derived_uuid",
        AsyncMock(return_value=SimpleNamespace(id="ag_helper", name="helper", metadata={})),
    )
    monkeypatch.setattr(repos_module, "is_platform_admin", lambda _interaction: admin)
    monkeypatch.setattr(
        repos_module,
        "admin_caller",
        lambda user: AdminCaller(platform_user_id=str(user.id), is_server_admin=admin),
    )
    interaction = MagicMock()
    interaction.guild_id = _GUILD
    interaction.channel_id = 55
    interaction.application_id = 1234
    interaction.token = "interaction-token"
    interaction.guild.name = "Guild"
    interaction.response.defer = AsyncMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user
    interaction.user.display_name = f"user-{user}"
    return interaction


def _state(account_id: uuid.UUID) -> PanelState:
    return PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        guild_id=_GUILD,
        channel_id=456,
        selected_agent=_AGENT,
    )


async def _role_and_invitations(
    sessions: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> tuple[Role, list[Mapping[str, Any]]]:
    async with sessions() as session:
        account = await get_account(session, account_id)
        rows = await session.execute(
            text(
                "SELECT agent_id, agent_ma_id, requester_platform_user_id "
                "FROM github_connect_invitations WHERE requester_account_id = :account"
            ),
            {"account": account_id},
        )
    assert account is not None
    return account.role, list(rows.mappings())


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [1, 2])
async def test_an_owner_opens_the_panel_and_gets_a_link_without_being_promoted(
    monkeypatch: pytest.MonkeyPatch, world, db_session_factory, user: int
) -> None:
    _, accounts, runtime = world
    interaction = _click(monkeypatch, user)
    state = _state(accounts[user])
    view = GitHubReposView(
        state,
        runtime=runtime,
        allowed_user_id=user,
        agent=_AGENT,
        panel=GrantsPanel(repos=(), working_repo=None),
    )
    monkeypatch.setattr(
        repos_module,
        "get_account",
        AsyncMock(return_value=SimpleNamespace(id=accounts[user], tenant_id=world[0])),
    )
    assert await view.allowed(interaction)
    url = await mint_agent_connect_url(interaction, runtime=runtime, state=state, agent=_AGENT)
    assert url.startswith("https://mcp.test/oauth/github/connect/")
    role, [invitation] = await _role_and_invitations(db_session_factory, accounts[user])
    assert role == Role.USER, "an owner's link never promotes them"
    assert invitation["agent_ma_id"] == "ag_helper"


@pytest.mark.asyncio
async def test_a_member_is_refused_the_panel_and_no_link_is_minted(
    monkeypatch: pytest.MonkeyPatch, world, db_session_factory
) -> None:
    tenant_id, accounts, runtime = world
    interaction = _click(monkeypatch, 3)
    state = _state(accounts[3])
    view = GitHubReposView(
        state,
        runtime=runtime,
        allowed_user_id=3,
        agent=_AGENT,
        panel=GrantsPanel(repos=(), working_repo=None),
    )
    monkeypatch.setattr(
        repos_module,
        "get_account",
        AsyncMock(return_value=SimpleNamespace(id=accounts[3], tenant_id=tenant_id)),
    )
    assert not await view.allowed(interaction)
    with pytest.raises(ValueError, match="Only an admin"):
        await mint_agent_connect_url(interaction, runtime=runtime, state=state, agent=_AGENT)
    assert (await _role_and_invitations(db_session_factory, accounts[3]))[1] == []
