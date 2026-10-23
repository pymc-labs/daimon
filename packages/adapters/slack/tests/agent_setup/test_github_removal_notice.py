"""GitHub removal notices stay in the admin's setup channel."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.slack.agent_setup import github_removal


class _Sessions:
    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_removal_notice_uses_ephemeral_button_without_dm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_removal, "resolve_is_platform_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(
        github_removal,
        "claim_notice",
        AsyncMock(return_value=SimpleNamespace(account_login="example")),
    )
    finish = AsyncMock()
    monkeypatch.setattr(github_removal, "finish_notice", finish)
    monkeypatch.setattr(github_removal, "sync_connect_admin", AsyncMock())
    connect_link = AsyncMock(return_value="https://mcp.test/connect")
    monkeypatch.setattr(github_removal, "connect_link", connect_link)
    send_link = AsyncMock()
    monkeypatch.setattr(github_removal, "send_link", send_link)
    client = SimpleNamespace(conversations_open=AsyncMock(), chat_postMessage=AsyncMock())
    runtime = SimpleNamespace(
        sessionmaker=_Sessions(),
        settings=SimpleNamespace(mcp=SimpleNamespace(app_root_url="https://mcp.test")),
    )
    await github_removal.send_pending_notice(
        runtime,  # pyright: ignore[reportArgumentType]
        client,  # pyright: ignore[reportArgumentType]
        team_id="T1",
        channel_id="C1",
        user_id="U1",
        response_url="https://hooks.slack.test/response",
    )
    assert connect_link.await_args is not None
    assert connect_link.await_args.kwargs["origin_followup_token"] == (
        "https://hooks.slack.test/response"
    )
    assert send_link.await_args is not None
    assert send_link.await_args.kwargs["channel_id"] == "C1"
    assert send_link.await_args.kwargs["user_id"] == "U1"
    assert send_link.await_args.kwargs["url"] == "https://mcp.test/connect"
    assert send_link.await_args.kwargs["app_root_url"] == "https://mcp.test"
    client.conversations_open.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()
    assert finish.await_args is not None
    assert finish.await_args.kwargs["delivered"] is True


async def test_delegate_pending_removal_notice_never_promotes_or_mints_a_link(
    db_session,
    db_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re
    from unittest.mock import MagicMock

    from aioresponses import aioresponses
    from daimon.adapters.slack.admin import WorkspaceWebClient, resolve_is_admin
    from daimon.core.admin_actions import DelegatedAction
    from daimon.core.native_admins import read_stored_admin
    from daimon.core.stores.accounts import get_account
    from daimon.core.stores.daimon_admins import change_grant
    from daimon.core.stores.domain import Role
    from daimon.testing.factories import (
        make_github_removal_notice,
        make_platform_principal,
        make_tenant,
    )

    tenant = await make_tenant(db_session, platform="slack", workspace_id="T0REVIEW3")
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="slack", external_id="U0DELEGATE"
    )
    await change_grant(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        subject_kind="user",
        subject_id=principal.external_id,
        actor_user_id="U0OWNER",
        is_platform_admin=True,
    )
    await make_github_removal_notice(db_session, tenant_id=tenant.id)
    await db_session.commit()
    runtime = MagicMock(sessionmaker=db_session_factory)
    client = WorkspaceWebClient(token="test").bind_workspace(runtime, "T0REVIEW3")
    link = AsyncMock(wraps=github_removal.connect_link)
    claim = AsyncMock(wraps=github_removal.claim_notice)
    sent = AsyncMock()
    monkeypatch.setattr(github_removal, "connect_link", link)
    monkeypatch.setattr(github_removal, "claim_notice", claim)
    monkeypatch.setattr(github_removal, "send_link", sent)
    with aioresponses() as api:
        api.get(
            re.compile(r"https://slack.com/api/users.info.*"),
            payload={
                "ok": True,
                "user": {
                    "id": principal.external_id,
                    "team_id": "T0REVIEW3",
                    "is_admin": False,
                    "deleted": False,
                    "is_bot": False,
                },
            },
            repeat=True,
        )
        assert await resolve_is_admin(
            client, user_id=principal.external_id, action=DelegatedAction.AGENT_SETTINGS
        )
        await github_removal.send_pending_notice(
            runtime,
            client,
            team_id="T0REVIEW3",
            channel_id="C1",
            user_id=principal.external_id,
        )
    claim.assert_not_awaited()
    link.assert_not_awaited()
    sent.assert_not_awaited()
    account = await get_account(db_session, principal.account_id)
    assert account is not None and account.role is Role.USER
    stored = await read_stored_admin(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        account_id=principal.account_id,
        platform_user_id=principal.external_id,
    )
    assert not stored.is_admin
