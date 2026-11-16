"""`/dm-me` and later DM turns answer to the source channel's budget.

Admission, the live role lookup and the DM store are patched on the module.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import pytest
from daimon.adapters.slack import direct_messages as dm_module
from daimon.adapters.slack.blockkit import ERROR_COLOR, NEUTRAL_COLOR
from daimon.core.direct_messages import ColdDmNotices
from daimon.core.dm_debounce import DmDebouncer
from daimon.core.errors import TurnError
from daimon.core.stores.domain import Role
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.factories import make_account, make_dm_conversation, make_tenant
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import shown_text

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
    client.reactions_add = client.reactions_remove = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=role))
    save = AsyncMock()
    monkeypatch.setattr(dm_module, "set_dms", save)
    runtime = MagicMock()
    await dm_module.handle_dm_command(
        runtime,
        {"team_id": _TEAM, "user_id": "U1", "channel_id": _CHANNEL, "text": action},
    )
    sent = client.chat_postEphemeral.await_args.kwargs
    assert shown_text(sent) == expected
    colour = sent["attachments"][0]["color"]
    assert colour == (ERROR_COLOR if role is Role.USER else NEUTRAL_COLOR)
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
    client.reactions_add = client.reactions_remove = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "3.1"})
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(dm_module, "reply_to_dm", AsyncMock(return_value="A" * 7001))
    runtime = MagicMock()
    runtime.dm_bursts = DmDebouncer()
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
    client.reactions_add = client.reactions_remove = AsyncMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(
        dm_module,
        "reply_to_dm",
        AsyncMock(side_effect=AdmissionDenied(reason="channel_budget_exceeded")),
    )
    runtime = MagicMock()
    runtime.dm_bursts = DmDebouncer()
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
        return MagicMock(source_sealed=False, source_shared=False)

    client = MagicMock()
    client.reactions_add = client.reactions_remove = AsyncMock()
    client.conversations_history = AsyncMock(return_value={"messages": []})
    client.conversations_open = AsyncMock(return_value={"channel": {"id": "D_NEW"}})
    client.chat_postMessage = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(dm_module, "require_dm_enabled", AsyncMock())
    monkeypatch.setattr(dm_module, "sealed_channel_ids", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(dm_module, "admit", admit)
    start_dm = AsyncMock()
    monkeypatch.setattr(dm_module, "start_dm", start_dm)

    await dm_module.handle_dm_command(
        MagicMock(), {"team_id": _TEAM, "user_id": "U1", "channel_id": _CHANNEL, "text": ""}
    )

    (call,) = admitted
    assert (call["is_dm"], call["dm_source_channel_id"]) == (True, _CHANNEL), (
        "admitted against the channel /dm-me ran in"
    )
    reply = shown_text(client.chat_postEphemeral.await_args.kwargs)
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
    client.reactions_add = client.reactions_remove = AsyncMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(
        dm_module,
        "reply_to_dm",
        AsyncMock(side_effect=AdmissionDenied(reason="channel_budget_exceeded")),
    )
    runtime = MagicMock()
    runtime.dm_bursts = DmDebouncer()
    runtime.sessionmaker = db_session_factory
    event = {"type": "message", "channel_type": "im", "channel": "D1", "user": "U1"}

    await dm_module.handle_direct_message(
        runtime, {**event, "ts": "1.1", "text": "hi"}, team_id=_TEAM
    )

    reply = shown_text(client.chat_postMessage.await_args.kwargs)
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
    client.reactions_add = client.reactions_remove = AsyncMock()
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
    runtime.dm_bursts = DmDebouncer()
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
    client.reactions_add = client.reactions_remove = AsyncMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(
        dm_module,
        "reply_to_dm",
        AsyncMock(side_effect=TurnError(kind="upstream", cause=provider_error)),
    )
    runtime = MagicMock()
    runtime.dm_bursts = DmDebouncer()
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

    text = shown_text(client.chat_postMessage.await_args.kwargs)
    assert text.startswith(
        "Daimon couldn't reach its AI service.\n\nTry again in a minute.\n\n_Ref "
    )
    for private in ("sk-ant", "access_token", "api.internal.example"):
        assert private not in text, private


async def _burst_setup(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    seconds: float,
) -> tuple[MagicMock, MagicMock, AsyncMock]:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key=f"{_TEAM}:D_BURST",
            external_user_id="U1",
            workspace_id=_TEAM,
            source_channel_id=_CHANNEL,
        )
    client = MagicMock()
    client.reactions_add = AsyncMock()
    client.reactions_remove = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.1"})
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(dm_module, "debounce_seconds", AsyncMock(return_value=seconds))
    reply = AsyncMock(return_value="done")
    monkeypatch.setattr(dm_module, "reply_to_dm", reply)
    runtime = MagicMock()
    runtime.dm_bursts = DmDebouncer()
    runtime.sessionmaker = db_session_factory
    runtime.settings.agent_identity.enabled = False
    return runtime, client, reply


def _dm(text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": "D_BURST",
        "user": "U1",
        "ts": ts,
        "text": text,
    }


def _marks(call: AsyncMock) -> list[str]:
    return [item.kwargs["timestamp"] for item in call.await_args_list]


async def test_a_burst_of_dms_is_one_turn_after_the_quiet_gap(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, client, reply = await _burst_setup(db_session_factory, monkeypatch, 0.4)
    first = asyncio.create_task(
        dm_module.handle_direct_message(runtime, _dm("one", "1.1"), team_id=_TEAM)
    )
    await asyncio.sleep(0.1)
    await dm_module.handle_direct_message(runtime, _dm("two", "1.2"), team_id=_TEAM)
    await asyncio.sleep(0.05)
    assert _marks(client.reactions_add) == ["1.1", "1.2"], "marked as soon as each arrives"
    reply.assert_not_awaited()
    await asyncio.sleep(0.15)
    await dm_module.handle_direct_message(runtime, _dm("three", "1.3"), team_id=_TEAM)
    await first
    reply.assert_awaited_once()
    kwargs = reply.await_args.kwargs
    assert kwargs["text"] == "one\n\ntwo\n\nthree"
    assert (kwargs["message_id"], kwargs["merged_message_ids"]) == ("1.1", ("1.2", "1.3"))
    assert _marks(client.reactions_remove) == ["1.1", "1.2", "1.3"], "cleared after the turn"
    client.chat_postMessage.assert_awaited_once()


async def test_an_approval_reply_in_a_burst_is_its_own_turn_without_waiting(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _client, reply = await _burst_setup(db_session_factory, monkeypatch, 5.0)
    started = asyncio.get_running_loop().time()
    first = asyncio.create_task(
        dm_module.handle_direct_message(runtime, _dm("install it", "2.1"), team_id=_TEAM)
    )
    await asyncio.sleep(0.1)
    await dm_module.handle_direct_message(runtime, _dm("yes", "2.2"), team_id=_TEAM)
    await first
    assert asyncio.get_running_loop().time() - started < 4.0
    assert [call.kwargs["text"] for call in reply.await_args_list] == ["install it", "yes"]


async def test_a_lone_approval_reply_is_never_held(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, client, reply = await _burst_setup(db_session_factory, monkeypatch, 5.0)
    started = asyncio.get_running_loop().time()
    await dm_module.handle_direct_message(runtime, _dm("yes", "3.1"), team_id=_TEAM)
    assert asyncio.get_running_loop().time() - started < 4.0
    reply.assert_awaited_once()
    client.reactions_add.assert_not_awaited()


async def test_a_debounce_of_zero_answers_each_dm_at_once(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, client, reply = await _burst_setup(db_session_factory, monkeypatch, 0)
    await dm_module.handle_direct_message(runtime, _dm("hello", "4.1"), team_id=_TEAM)
    await dm_module.handle_direct_message(runtime, _dm("again", "4.2"), team_id=_TEAM)
    assert [call.kwargs["text"] for call in reply.await_args_list] == ["hello", "again"]
    client.reactions_add.assert_not_awaited()


def _slow_lookup(monkeypatch: pytest.MonkeyPatch, *, fails: bool = False) -> asyncio.Event:
    """Hold the conversation lookup until the returned event is set; then run it or fail."""
    real = dm_module.get_conversation
    release = asyncio.Event()

    async def lookup(*args: Any, **kwargs: Any) -> Any:
        await release.wait()
        if fails:
            raise OperationalError("SELECT", {}, Exception("connection lost"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(dm_module, "get_conversation", lookup)
    return release


def _notices(client: MagicMock) -> list[str]:
    return [shown_text(call.kwargs) for call in client.chat_postMessage.await_args_list]


async def test_a_failed_turn_takes_the_eyes_off_the_burst(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn error is told once; every message held for it loses its mark."""
    runtime, client, reply = await _burst_setup(db_session_factory, monkeypatch, 0.2)
    reply.side_effect = AdmissionDenied(reason="channel_budget_exceeded")
    first = asyncio.create_task(
        dm_module.handle_direct_message(runtime, _dm("one", "5.1"), team_id=_TEAM)
    )
    await asyncio.sleep(0.05)
    await dm_module.handle_direct_message(runtime, _dm("two", "5.2"), team_id=_TEAM)
    await first
    assert len(_notices(client)) == 1, "one notice for the failed turn, no second one"
    assert dm_module._NOT_SENT not in _notices(client), "the failure's own notice tells it"  # pyright: ignore[reportPrivateUsage]
    assert _marks(client.reactions_remove) == ["5.1", "5.2"], "both marks come off"


async def test_an_unhandled_error_tells_the_rest_of_the_burst_it_was_not_sent(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error no turn handles still answers every message taken in, then propagates."""
    runtime, client, reply = await _burst_setup(db_session_factory, monkeypatch, 0.2)
    reply.side_effect = RuntimeError("bug")
    first = asyncio.create_task(
        dm_module.handle_direct_message(runtime, _dm("one", "6.1"), team_id=_TEAM)
    )
    await asyncio.sleep(0.05)
    await dm_module.handle_direct_message(runtime, _dm("yes", "6.2"), team_id=_TEAM)
    with pytest.raises(RuntimeError):
        await first
    reply.assert_awaited_once()
    assert _notices(client) == [dm_module._NOT_SENT], "one notice for both messages"  # pyright: ignore[reportPrivateUsage]
    assert _marks(client.reactions_remove) == ["6.1", "6.2"], "both marks come off"


async def test_a_failed_lookup_tells_the_whole_burst_once(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message that joined during a failed lookup is covered by its notice."""
    runtime, client, reply = await _burst_setup(db_session_factory, monkeypatch, 5.0)
    release = _slow_lookup(monkeypatch, fails=True)
    first = asyncio.create_task(
        dm_module.handle_direct_message(runtime, _dm("one", "7.1"), team_id=_TEAM)
    )
    await asyncio.sleep(0.05)
    await dm_module.handle_direct_message(runtime, _dm("two", "7.2"), team_id=_TEAM)
    release.set()
    await first
    reply.assert_not_awaited()
    assert len(_notices(client)) == 1, "one error notice answers both"
    assert not runtime.dm_bursts.join((_TEAM, "D_BURST", "U1"), _dm("three", "7.3")), (
        "the window is closed once its notice is sent"
    )


async def test_a_message_joining_a_burst_with_no_conversation_gets_the_one_notice(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold notice speaks for the whole burst; nothing is told it wasn't sent."""
    runtime, client, reply = await _burst_setup(db_session_factory, monkeypatch, 5.0)
    monkeypatch.setattr(dm_module, "_COLD_DM_NOTICES", ColdDmNotices())
    release = _slow_lookup(monkeypatch)
    first = asyncio.create_task(
        dm_module.handle_direct_message(
            runtime, {**_dm("one", "8.1"), "channel": "D_NONE"}, team_id=_TEAM
        )
    )
    await asyncio.sleep(0.05)
    await dm_module.handle_direct_message(
        runtime, {**_dm("two", "8.2"), "channel": "D_NONE"}, team_id=_TEAM
    )
    release.set()
    await first
    reply.assert_not_awaited()
    assert len(_notices(client)) == 1, "only the notice that there is nothing to continue"
    assert dm_module._NOT_SENT not in _notices(client)  # pyright: ignore[reportPrivateUsage]


async def test_with_the_wait_off_a_message_sent_during_the_lookup_is_marked_and_merged(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off answers at once, but a message that already joined rides along, marked."""
    runtime, client, reply = await _burst_setup(db_session_factory, monkeypatch, 0)
    release = _slow_lookup(monkeypatch)
    first = asyncio.create_task(
        dm_module.handle_direct_message(runtime, _dm("one", "9.1"), team_id=_TEAM)
    )
    await asyncio.sleep(0.05)
    await dm_module.handle_direct_message(runtime, _dm("two", "9.2"), team_id=_TEAM)
    release.set()
    await first
    assert [call.kwargs["text"] for call in reply.await_args_list] == ["one\n\ntwo"]
    assert _marks(client.reactions_add) == ["9.2"], "only the one that was held is marked"
    assert _marks(client.reactions_remove) == ["9.2"], "and its mark comes off"
