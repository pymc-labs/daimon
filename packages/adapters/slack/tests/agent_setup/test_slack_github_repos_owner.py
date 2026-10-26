"""The Slack repos modal offers an owner of the agent the Connect link, read at the click."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack.agent_setup import github_repos
from daimon.adapters.slack.agent_setup import github_repos_actions as actions_module
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.core.github_panel import GrantsPanel
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.roster import RosterAgent
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM = "T-REPOS"
_AGENT = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="m", is_built_in=False)


@pytest.fixture
async def runtime(db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]):
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    assert tenant.id == derive_tenant_uuid(platform="slack", workspace_id=_TEAM)
    owner = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, tenant=tenant, account=owner, platform="slack", external_id="U-OWNER"
    )
    member = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, tenant=tenant, account=member, platform="slack", external_id="U-MEMBER"
    )
    await agent_owners.set_owner(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT.ma_agent_id),
        owner_account_id=owner.id,
        source="admin_assigned",
    )
    await db_session.commit()
    return SimpleNamespace(
        anthropic=object(),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        settings=object(),
    )


async def _open(
    monkeypatch: pytest.MonkeyPatch, runtime: Any, *, user: str, admin: bool = False
) -> tuple[AsyncMock, AsyncMock]:
    client = MagicMock()
    client.views_push = AsyncMock()
    connect = AsyncMock(return_value="https://mcp.test/oauth/github/connect/token")
    sync = AsyncMock()
    monkeypatch.setattr(actions_module, "resolve_is_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(
        actions_module,
        "load_panel_roster",
        AsyncMock(return_value=SimpleNamespace(rows=[_AGENT])),
    )
    monkeypatch.setattr(
        actions_module,
        "load_grants_panel",
        AsyncMock(return_value=GrantsPanel(repos=(), working_repo=None)),
    )
    monkeypatch.setattr(
        actions_module,
        "find_agent_by_derived_uuid",
        AsyncMock(return_value=SimpleNamespace(id="ag_helper", name="helper", metadata={})),
    )
    monkeypatch.setattr(actions_module, "connect_link", connect)
    monkeypatch.setattr(actions_module, "sync_connect_admin", sync)
    handled = await actions_module.handle(
        runtime,
        client,
        {"trigger_id": "trigger", "view": {"id": "V1"}},
        action={"action_id": github_repos.ACTION_OPEN, "value": "helper"},
        meta=PanelMetadata(team_id=_TEAM, channel_id="C1", view="details", agent_name="helper"),
        team_id=_TEAM,
        user_id=user,
    )
    assert handled
    client.views_push.assert_awaited_once()
    return connect, sync


@pytest.mark.asyncio
async def test_an_owner_gets_the_link_without_the_admin_promotion(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    connect, sync = await _open(monkeypatch, runtime, user="U-OWNER")
    kwargs = connect.await_args.kwargs
    assert kwargs["verified_tenant_admin"] is False
    assert kwargs["verified_agent_manager"] is True
    assert kwargs["agent_ma_id"] == "ag_helper"
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_member_gets_no_link(monkeypatch: pytest.MonkeyPatch, runtime) -> None:
    connect, sync = await _open(monkeypatch, runtime, user="U-MEMBER")
    connect.assert_not_awaited()
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_admin_still_gets_the_promoted_admin_link(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    connect, sync = await _open(monkeypatch, runtime, user="U-ADMIN", admin=True)
    assert connect.await_args.kwargs["verified_tenant_admin"] is True
    assert connect.await_args.kwargs["verified_agent_manager"] is False
    sync.assert_awaited_once()
