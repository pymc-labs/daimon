"""An agent's owner gets the Connect GitHub link for their own agent, and is never promoted."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.slack import github_connect as github_command
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.testing.adapters.slack.notices import shown_text
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM = "T-OWNERS"
_AGENT = SimpleNamespace(name="ResearchBot", id="ma-agent")
_OTHER = SimpleNamespace(name="OtherBot", id="ma-other")


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
    *,
    user: str,
    agent: str,
    admin: bool = False,
) -> AsyncMock:
    client = SimpleNamespace(
        users_info=AsyncMock(return_value={"user": {"team_id": _TEAM}}),
        chat_postEphemeral=AsyncMock(),
    )
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
    agents = [_AGENT, _OTHER]

    async def live(_client: object, *, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> object | None:
        for row in agents:
            if derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.id) == agent_id:
                return SimpleNamespace(id=row.id, name=row.name, metadata={})
        return None

    monkeypatch.setattr(github_command, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(github_command, "resolve_is_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(github_command, "list_agents_by_tenant", AsyncMock(return_value=agents))
    monkeypatch.setattr(github_command, "find_agent_by_derived_uuid", live)
    await github_command.handle_github_command(
        cast(SlackRuntime, runtime),
        {
            "team_id": _TEAM,
            "user_id": user,
            "channel_id": "C1",
            "text": f"connect {agent}",
            "response_url": "https://hooks.slack.com/services/test",
        },
    )
    return client.chat_postEphemeral


@pytest.fixture
async def people(db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]):
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    assert tenant.id == derive_tenant_uuid(platform="slack", workspace_id=_TEAM)
    accounts: dict[str, uuid.UUID] = {}
    for user in ("U-OWNER", "U-CO", "U-MEMBER", "U-ADMIN"):
        account = await make_account(db_session, tenant=tenant)
        await make_platform_principal(
            db_session, tenant=tenant, account=account, platform="slack", external_id=user
        )
        accounts[user] = account.id
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT.id)
    await agent_owners.set_owner(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        owner_account_id=accounts["U-OWNER"],
        source="admin_assigned",
    )
    await agent_owners.add_co_owner(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=accounts["U-CO"],
        added_by_account_id=None,
    )
    await db_session.commit()
    return accounts, agent_id


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
@pytest.mark.parametrize("user", ["U-OWNER", "U-CO"])
async def test_an_owner_gets_a_link_for_their_agent_and_stays_a_member(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    people,
    user: str,
) -> None:
    accounts, agent_id = people
    post = await _run(monkeypatch, db_session_factory, user=user, agent="ResearchBot")
    kwargs = post.await_args.kwargs
    button = next(
        block for block in kwargs["attachments"][0]["blocks"] if block["type"] == "actions"
    )["elements"][0]
    assert button["url"].startswith("https://mcp.test/oauth/github/connect/")
    role, invitations = await _role_and_invitations(db_session_factory, accounts[user])
    assert role == Role.USER, "an owner is never promoted to admin"
    [invitation] = invitations
    assert invitation["agent_id"] == agent_id and invitation["requester_platform_user_id"] == user


@pytest.mark.asyncio
async def test_an_owner_is_refused_for_another_agent_and_a_member_for_any(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    people,
) -> None:
    accounts, _ = people
    for user, agent in (("U-OWNER", "OtherBot"), ("U-MEMBER", "ResearchBot")):
        post = await _run(monkeypatch, db_session_factory, user=user, agent=agent)
        assert "Only an admin or an owner of this agent" in shown_text(post.await_args.kwargs)
        role, invitations = await _role_and_invitations(db_session_factory, accounts[user])
        assert role == Role.USER and invitations == []


@pytest.mark.asyncio
async def test_an_admin_is_still_promoted_and_may_connect_any_agent(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    people,
) -> None:
    accounts, _ = people
    post = await _run(monkeypatch, db_session_factory, user="U-ADMIN", agent="OtherBot", admin=True)
    assert post.await_args.kwargs["attachments"]
    role, invitations = await _role_and_invitations(db_session_factory, accounts["U-ADMIN"])
    assert role == Role.ADMIN and len(invitations) == 1
