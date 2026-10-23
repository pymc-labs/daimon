"""Delegation remains separate from Slack's live platform bit."""

from __future__ import annotations

import re
from unittest.mock import MagicMock

from aioresponses import aioresponses
from daimon.adapters.slack.admin import (
    WorkspaceWebClient,
    resolve_is_admin,
    resolve_is_platform_admin,
)
from daimon.core.admin_actions import DelegatedAction
from daimon.core.stores.daimon_admins import change_grant
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_granted_user_passes_then_revoke_refuses_same_client(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T123")
    key = dict(
        tenant_id=tenant.id,
        platform="slack",
        subject_kind="user",
        subject_id="U123",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await change_grant(db_session, **key)
    await db_session.commit()
    client = WorkspaceWebClient(token="test").bind_workspace(
        MagicMock(sessionmaker=db_session_factory), "T123"
    )
    with aioresponses() as api:
        api.get(
            re.compile(r"https://slack.com/api/users.info.*"),
            payload={
                "ok": True,
                "user": {
                    "id": "U123",
                    "team_id": "T123",
                    "deleted": False,
                    "is_bot": False,
                    "is_admin": False,
                },
            },
            repeat=True,
        )
        assert await resolve_is_admin(client, user_id="U123", action=DelegatedAction.AGENT_SETTINGS)
        assert not await resolve_is_platform_admin(client, user_id="U123"), (
            "money and grants remain platform-only"
        )
        await change_grant(db_session, **key, revoke=True)
        await db_session.commit()
        assert not await resolve_is_admin(
            client, user_id="U123", action=DelegatedAction.AGENT_SETTINGS
        )


async def test_delegated_admin_cannot_forge_a_grant_or_revoke_click(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from unittest.mock import AsyncMock

    from daimon.adapters.slack.agent_setup.admins import (
        ACTION_GRANT,
        ACTION_REVOKE,
        handle_admin_action,
    )
    from daimon.adapters.slack.agent_setup.state import PanelMetadata
    from daimon.core.stores.daimon_admins import list_admins
    from daimon.core.stores.security_audit import list_events

    tenant = await make_tenant(db_session, platform="slack", workspace_id="T123")
    await change_grant(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        subject_kind="user",
        subject_id="U123",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await db_session.commit()
    runtime = MagicMock(sessionmaker=db_session_factory)
    client = WorkspaceWebClient(token="test").bind_workspace(runtime, "T123")
    client.chat_postEphemeral = AsyncMock()
    meta = PanelMetadata(team_id="T123", channel_id="C123", view="admins")
    payload = {
        "view": {
            "id": "view",
            "state": {"values": {"admin_subject": {"user": {"selected_user": "UOTHER"}}}},
        }
    }
    with aioresponses() as api:
        api.get(
            re.compile(r"https://slack.com/api/users.info.*"),
            payload={
                "ok": True,
                "user": {
                    "id": "U123",
                    "team_id": "T123",
                    "deleted": False,
                    "is_bot": False,
                    "is_admin": False,
                },
            },
            repeat=True,
        )
        for action in (ACTION_GRANT, ACTION_REVOKE):
            await handle_admin_action(
                runtime,
                client,
                payload=payload,
                meta=meta,
                user_id="U123",
                team_id="T123",
                action_id=action,
            )
    assert client.chat_postEphemeral.await_count == 2
    assert len(await list_admins(db_session, tenant_id=tenant.id)) == 1
    assert len(await list_events(db_session, tenant_id=tenant.id)) == 1


async def test_delegate_is_refused_at_github_navigation_boundary(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock

    from daimon.adapters.slack.agent_setup import actions, panel_views
    from daimon.adapters.slack.agent_setup.state import PanelMetadata

    tenant = await make_tenant(db_session, platform="slack", workspace_id="T123")
    await change_grant(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        subject_kind="user",
        subject_id="U123",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await db_session.commit()
    client = WorkspaceWebClient(token="test").bind_workspace(
        MagicMock(sessionmaker=db_session_factory), "T123"
    )
    ephemeral = AsyncMock()
    monkeypatch.setattr(actions, "post_ephemeral", ephemeral)
    with aioresponses() as api:
        api.get(
            re.compile(r"https://slack.com/api/users.info.*"),
            payload={
                "ok": True,
                "user": {
                    "id": "U123",
                    "team_id": "T123",
                    "deleted": False,
                    "is_bot": False,
                    "is_admin": False,
                },
            },
            repeat=True,
        )
        assert await resolve_is_admin(client, user_id="U123", action=DelegatedAction.AGENT_SETTINGS)
        await actions._dispatch_panel_action(
            MagicMock(sessionmaker=db_session_factory),
            client,
            {},
            action={},
            action_id=panel_views.ACTION_GITHUB_PERSONAL_LINK,
            meta=PanelMetadata(team_id="T123", channel_id="C123", view="agents"),
            tenant_id=tenant.id,
            team_id="T123",
            user_id="U123",
        )
    assert "platform admin" in ephemeral.await_args.kwargs["text"]
