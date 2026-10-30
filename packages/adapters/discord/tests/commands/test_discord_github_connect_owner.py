"""An agent's owner gets the Connect GitHub link for their own agent, and is never promoted."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.discord.commands import github as github_command
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = "9001"
_AGENT = SimpleNamespace(name="ResearchBot", id="ma-agent")
_OTHER = SimpleNamespace(name="OtherBot", id="ma-other")


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    user: int,
    agent: str,
    admin: bool = False,
) -> AsyncMock:
    runtime = SimpleNamespace(
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
        anthropic=object(),
        sessionmaker=sessions,
        deployment_default=DeploymentDefault(),
    )
    interaction = AsyncMock()
    interaction.user.id = user
    interaction.guild_id = int(_GUILD)
    interaction.channel_id = 55
    interaction.application_id = 456
    interaction.token = "interaction-token"
    interaction.client.runtime = runtime
    agents = [_AGENT, _OTHER]

    async def live(_client: object, *, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> object | None:
        for row in agents:
            if derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.id) == agent_id:
                return SimpleNamespace(id=row.id, name=row.name, metadata={})
        return None

    monkeypatch.setattr(github_command, "is_guild_admin", lambda _interaction: admin)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(
        github_command, "resolve_tenant_for_interaction", AsyncMock(return_value=tenant_id)
    )
    monkeypatch.setattr(github_command, "list_agents_by_tenant", AsyncMock(return_value=agents))
    monkeypatch.setattr(github_command, "find_agent_by_derived_uuid", live)
    cog = github_command.GitHubCog(AsyncMock())
    await github_command.GitHubCog.connect.callback.__wrapped__(  # type: ignore[attr-defined]
        cog, interaction, agent
    )
    return interaction.followup.send


@pytest.fixture
async def people(db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]):
    tenant = await make_tenant(db_session, platform="discord", workspace_id=_GUILD)
    accounts: dict[int, uuid.UUID] = {}
    for user in (1, 2, 3, 4):  # owner, co-owner, member, admin
        account = await make_account(db_session, tenant=tenant)
        await make_platform_principal(
            db_session, tenant=tenant, account=account, platform="discord", external_id=str(user)
        )
        accounts[user] = account.id
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT.id)
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
    return tenant.id, accounts, agent_id


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
async def test_an_owner_gets_a_link_for_their_agent_and_stays_a_member(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    people,
    user: int,
) -> None:
    tenant_id, accounts, agent_id = people
    send = await _run(monkeypatch, db_session_factory, tenant_id, user=user, agent="ResearchBot")
    kwargs = send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["view"].children[0].url.startswith("https://mcp.test/oauth/github/connect/")
    role, invitations = await _role_and_invitations(db_session_factory, accounts[user])
    assert role == Role.USER, "an owner is never promoted to admin"
    [invitation] = invitations
    assert invitation["agent_id"] == agent_id and invitation["requester_platform_user_id"] == str(
        user
    )


@pytest.mark.asyncio
async def test_an_owner_is_refused_for_another_agent_and_a_member_for_any(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    people,
) -> None:
    tenant_id, accounts, _ = people
    for user, agent in ((1, "OtherBot"), (3, "ResearchBot")):
        send = await _run(monkeypatch, db_session_factory, tenant_id, user=user, agent=agent)
        assert (
            "Only an admin or an owner of this agent" in send.await_args.kwargs["embed"].description
        )
        role, invitations = await _role_and_invitations(db_session_factory, accounts[user])
        assert role == Role.USER and invitations == []


@pytest.mark.asyncio
async def test_an_admin_is_still_promoted_and_may_connect_any_agent(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    people,
) -> None:
    tenant_id, accounts, _ = people
    send = await _run(
        monkeypatch, db_session_factory, tenant_id, user=4, agent="OtherBot", admin=True
    )
    assert send.await_args.kwargs["view"].children[0].url
    role, invitations = await _role_and_invitations(db_session_factory, accounts[4])
    assert role == Role.ADMIN and len(invitations) == 1
