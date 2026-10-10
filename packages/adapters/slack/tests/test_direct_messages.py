"""`/dm` and later DM turns answer to the source channel's budget.

Admission, the live role lookup and the DM store are patched on the module.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import pytest
from daimon.adapters.slack import direct_messages as dm_module
from daimon.core.errors import TurnError
from daimon.core.stores.domain import Role
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.factories import make_account, make_dm_conversation, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM = "T_DM_BUDGET"
_CHANNEL = "C_SOURCE"


@pytest.mark.parametrize(
    ("role", "action", "expected"),
    [
        (Role.USER, "enable", "Only a workspace admin can turn Daimon DMs on or off here."),
        (Role.ADMIN, "enable", "Daimon DMs are on for this workspace."),
        (Role.ADMIN, "disable", "Daimon DMs are off for this workspace."),
    ],
)
async def test_dm_policy_replies_use_workspace_words(
    monkeypatch: pytest.MonkeyPatch, role: Role, action: str, expected: str
) -> None:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=role))
    save = AsyncMock()
    monkeypatch.setattr(dm_module, "set_dm_enabled", save)
    runtime = MagicMock()
    await dm_module.handle_dm_command(
        runtime,
        {"team_id": _TEAM, "user_id": "U1", "channel_id": _CHANNEL, "text": action},
    )
    assert client.chat_postEphemeral.await_args.kwargs["text"] == expected
    assert save.await_count == (1 if role is Role.ADMIN else 0)


async def test_reply_in_dm_thread_stays_in_that_thread(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key=f"{_TEAM}:D_THREAD",
            external_user_id="U1",
            workspace_id=_TEAM,
            source_channel_id=_CHANNEL,
        )
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "3.1"})
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(dm_module, "reply_to_dm", AsyncMock(return_value="A" * 7001))
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    runtime.settings.agent_identity.enabled = False

    await dm_module.handle_direct_message(
        runtime,
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D_THREAD",
            "user": "U1",
            "ts": "2.1",
            "thread_ts": "1.1",
            "text": "follow-up in the thread",
        },
        team_id=_TEAM,
    )

    assert client.chat_postMessage.await_count == 3
    assert all(
        call.kwargs["thread_ts"] == "1.1" for call in client.chat_postMessage.await_args_list
    )


async def test_error_in_dm_thread_stays_in_that_thread(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key=f"{_TEAM}:D_THREAD_ERROR",
            external_user_id="U1",
            workspace_id=_TEAM,
            source_channel_id=_CHANNEL,
        )
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(
        dm_module,
        "reply_to_dm",
        AsyncMock(side_effect=AdmissionDenied(reason="channel_budget_exceeded")),
    )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory

    await dm_module.handle_direct_message(
        runtime,
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D_THREAD_ERROR",
            "user": "U1",
            "ts": "2.1",
            "thread_ts": "1.1",
            "text": "follow-up in the thread",
        },
        team_id=_TEAM,
    )

    assert client.chat_postMessage.await_args.kwargs["thread_ts"] == "1.1"


@pytest.mark.parametrize("over_budget", [True, False])
async def test_dm_admits_and_records_the_channel_it_ran_in(
    monkeypatch: pytest.MonkeyPatch, over_budget: bool
) -> None:
    admitted: list[dict[str, Any]] = []

    async def admit(deps: object, **kwargs: Any) -> MagicMock:
        admitted.append(kwargs)
        if over_budget:
            raise AdmissionDenied(reason="channel_budget_exceeded")
        return MagicMock(source_sealed=False)

    client = MagicMock()
    client.conversations_history = AsyncMock(return_value={"messages": []})
    client.conversations_open = AsyncMock(return_value={"channel": {"id": "D_NEW"}})
    client.chat_postMessage = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(dm_module, "require_dm_enabled", AsyncMock())
    monkeypatch.setattr(dm_module, "sealed_channel_ids", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(dm_module, "admit", admit)
    monkeypatch.setattr(dm_module, "user_group_ids", AsyncMock(return_value=frozenset()))
    start_dm = AsyncMock()
    monkeypatch.setattr(dm_module, "start_dm", start_dm)

    await dm_module.handle_dm_command(
        MagicMock(), {"team_id": _TEAM, "user_id": "U1", "channel_id": _CHANNEL, "text": ""}
    )

    (call,) = admitted
    assert (call["is_dm"], call["dm_source_channel_id"]) == (True, _CHANNEL), (
        "admitted against the channel /dm ran in"
    )
    reply = client.chat_postEphemeral.await_args.kwargs["text"]
    if over_budget:
        assert reply.startswith("This channel's budget is used up."), reply
        client.conversations_open.assert_not_awaited()
        start_dm.assert_not_awaited()
    else:
        assert reply == "Ready in your DMs.", "a channel within its budget opens the DM"
        assert start_dm.await_args.kwargs["source_channel_id"] == _CHANNEL, (
            "the DM records its source channel"
        )


async def test_a_later_dm_turn_over_its_source_budget_tells_the_member(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key=f"{_TEAM}:D1",
            external_user_id="U1",
            workspace_id=_TEAM,
            source_channel_id=_CHANNEL,
        )
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(
        dm_module,
        "reply_to_dm",
        AsyncMock(side_effect=AdmissionDenied(reason="channel_budget_exceeded")),
    )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    event = {"type": "message", "channel_type": "im", "channel": "D1", "user": "U1"}

    await dm_module.handle_direct_message(
        runtime, {**event, "ts": "1.1", "text": "hi"}, team_id=_TEAM
    )

    reply = client.chat_postMessage.await_args.kwargs["text"]
    assert reply.startswith("This channel's budget is used up."), reply
    assert "thread_ts" not in client.chat_postMessage.await_args.kwargs


async def test_excluded_workspace_dm_skips_agent_identity_lookup(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key=f"{_TEAM}:D2",
            external_user_id="U1",
            workspace_id=_TEAM,
            source_channel_id=_CHANNEL,
        )
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    find_agent = AsyncMock()
    monkeypatch.setattr(dm_module, "find_agent_by_daimon_tag", find_agent)

    async def reply(*_args: Any, **kwargs: Any) -> str:
        await kwargs["on_agent"]("analyst")
        return "done"

    monkeypatch.setattr(dm_module, "reply_to_dm", reply)
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    runtime.settings.agent_identity.enabled = True
    runtime.settings.agent_identity.excluded_slack_team_ids = [_TEAM]
    await dm_module.handle_direct_message(
        runtime,
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D2",
            "user": "U1",
            "ts": "1.1",
            "text": "hi",
        },
        team_id=_TEAM,
    )
    find_agent.assert_not_awaited()
    kwargs = client.chat_postMessage.await_args.kwargs
    assert "username" not in kwargs and "icon_url" not in kwargs


async def test_a_failed_dm_turn_never_shows_the_provider_text(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn error carrying a provider body is shown as its cause lines and a Ref."""
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key=f"{_TEAM}:D_PROVIDER_ERROR",
            external_user_id="U1",
            workspace_id=_TEAM,
            source_channel_id=_CHANNEL,
        )
    leak = "see https://api.internal.example/v1?access_token=sk-ant-private-0000"
    provider_error = anthropic.APIStatusError(
        message=leak,
        response=httpx.Response(
            502, request=httpx.Request("POST", "https://api.anthropic.com"), text=leak
        ),
        body={"error": {"message": leak}},
    )
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(
        dm_module,
        "reply_to_dm",
        AsyncMock(side_effect=TurnError(kind="upstream", cause=provider_error)),
    )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory

    await dm_module.handle_direct_message(
        runtime,
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D_PROVIDER_ERROR",
            "user": "U1",
            "ts": "3.1",
            "text": "hello",
        },
        team_id=_TEAM,
    )

    text = client.chat_postMessage.await_args.kwargs["text"]
    assert text.startswith(
        "Daimon couldn't reach its AI service.\n\nTry again in a minute.\n\n_Ref "
    )
    for private in ("sk-ant", "access_token", "api.internal.example"):
        assert private not in text, private
