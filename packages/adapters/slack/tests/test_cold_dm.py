"""A DM with no conversation to continue is answered once, not met with silence."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import direct_messages as dm_module
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.direct_messages import ColdDmNotices
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.testing.factories import make_account, make_dm_conversation, make_tenant
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_OFF = (
    "Private chats with Daimon are off here.\n"
    "Mention me in a channel, or ask a workspace admin to run `/dm-me enable`."
)
_ON = (
    "To talk here, run `/dm-me` in a channel first and I'll continue with you here.\n"
    "Or mention me in any channel."
)


@pytest.fixture(autouse=True)
def fresh_notices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_module, "_COLD_DM_NOTICES", ColdDmNotices())


def _runtime(db_session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    return runtime


def _client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    return client


def _event(*, team_user: str = "U1", channel: str = "D_COLD", ts: str = "1.1") -> dict[str, Any]:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": channel,
        "user": team_user,
        "ts": ts,
        "text": "hello?",
    }


async def test_cold_dm_where_no_admin_chose_points_to_dm_me(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private chats are on unless an admin turned them off."""
    client = _client(monkeypatch)

    await dm_module.handle_direct_message(
        _runtime(db_session_factory), _event(), team_id="T_COLD_DEFAULT"
    )

    client.chat_postMessage.assert_awaited_once_with(channel="D_COLD", text=_ON)


async def test_cold_dm_with_private_chats_off_gets_the_two_line_answer(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await provision_tenant(db_session_factory, platform="slack", workspace_id="T_COLD_OFF")
    async with db_session_factory.begin() as session:
        await set_dm_enabled(session, tenant_id=tenant.tenant_id, enabled=False)
    client = _client(monkeypatch)

    await dm_module.handle_direct_message(
        _runtime(db_session_factory), _event(), team_id="T_COLD_OFF"
    )

    client.chat_postMessage.assert_awaited_once_with(channel="D_COLD", text=_OFF)


async def test_cold_dm_with_private_chats_on_points_to_dm_me(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await provision_tenant(db_session_factory, platform="slack", workspace_id="T_COLD_ON")
    async with db_session_factory.begin() as session:
        await set_dm_enabled(session, tenant_id=tenant.tenant_id, enabled=True)
    client = _client(monkeypatch)

    await dm_module.handle_direct_message(
        _runtime(db_session_factory), _event(), team_id="T_COLD_ON"
    )

    client.chat_postMessage.assert_awaited_once_with(channel="D_COLD", text=_ON)


async def test_dms_off_after_a_conversation_exists_gets_the_off_answer(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key="T_COLD_CONV:D_COLD",
            external_user_id="U1",
            workspace_id="T_COLD_CONV",
            source_channel_id="C1",
        )
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=False)
    client = _client(monkeypatch)

    await dm_module.handle_direct_message(
        _runtime(db_session_factory), _event(), team_id="T_COLD_CONV"
    )

    client.chat_postMessage.assert_awaited_once_with(channel="D_COLD", text=_OFF)


async def test_each_person_is_answered_once_per_process(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch)
    runtime = _runtime(db_session_factory)

    await dm_module.handle_direct_message(runtime, _event(ts="1.1"), team_id="T_COLD_ONCE")
    await dm_module.handle_direct_message(runtime, _event(ts="1.2"), team_id="T_COLD_ONCE")
    await dm_module.handle_direct_message(
        runtime, _event(team_user="U2", channel="D_OTHER"), team_id="T_COLD_ONCE"
    )

    assert [call.kwargs["channel"] for call in client.chat_postMessage.await_args_list] == [
        "D_COLD",
        "D_OTHER",
    ]


async def test_a_failed_answer_is_retried_on_the_next_message(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch)
    client.chat_postMessage.side_effect = [
        SlackApiError("not_in_channel", {"ok": False, "error": "not_in_channel"}),
        None,
    ]
    runtime = _runtime(db_session_factory)

    await dm_module.handle_direct_message(runtime, _event(ts="1.1"), team_id="T_COLD_RETRY")
    await dm_module.handle_direct_message(runtime, _event(ts="1.2"), team_id="T_COLD_RETRY")

    assert client.chat_postMessage.await_count == 2
