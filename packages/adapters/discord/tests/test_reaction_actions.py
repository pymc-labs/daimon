"""Reaction actions on Discord: the mark hook, the confirm listener's gates and its outcomes."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.discord import reaction_actions
from daimon.adapters.discord.reaction_actions import ReactionActionCog, mark_trigger_message
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.reaction_actions import CALL_PATH, Outcome, ReactionAction
from daimon.core.reaction_credential import verify
from daimon.core.turn.state import ToolUseBlock
from daimon.testing.settings_mock import mock_settings
from pydantic import SecretStr

_REAL_CLIENT = httpx.AsyncClient
_BOT = 999
_REACTOR = 100
_GUILD = 555
_CHANNEL = 456
_MESSAGE = 123
_MARK = 111
_CONFIRM = 222
_SECRET = "s" * 32
_WARNING = "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"

ACTION = ReactionAction(
    workspace=f"discord:{_GUILD}",
    server="tools",
    agent="Helper",
    mark_tool="propose",
    mark_emoji=f"mark:{_MARK}",
    confirm_tool="confirm",
    confirm_emoji=f"done:{_CONFIRM}",
)


def _feature(monkeypatch: pytest.MonkeyPatch, value: str = "on") -> AsyncMock:
    read = AsyncMock(return_value=ResolvedFeature("reaction_actions", value, "workspace"))
    monkeypatch.setattr(reaction_actions, "place_feature", read)
    return read


def _bot(actions: tuple[ReactionAction, ...] = (ACTION,), **user: Any) -> Any:
    settings = mock_settings()
    settings.mcp_identity.reaction_actions = list(actions)
    settings.mcp.app_root_url = "https://app.test"
    settings.mcp.jwt_secret = SecretStr(_SECRET)
    return SimpleNamespace(
        user=SimpleNamespace(id=_BOT),
        runtime=SimpleNamespace(sessionmaker=MagicMock(), settings=settings),
        get_channel=MagicMock(return_value=None),
        fetch_channel=AsyncMock(),
        get_user=MagicMock(return_value=user.get("dm_user")),
        fetch_user=AsyncMock(),
    )


def _reaction(emoji_id: int, *, me: bool = True) -> Any:
    return SimpleNamespace(emoji=discord.PartialEmoji(name="e", id=emoji_id), me=me)


def _message(*reactions: Any, thread: bool = False) -> Any:
    message = MagicMock(spec=discord.Message)
    message.reactions = list(reactions)
    message.guild = SimpleNamespace(id=_GUILD, me=SimpleNamespace(id=_BOT))
    message.channel = MagicMock(spec=discord.Thread if thread else discord.TextChannel)
    message.channel.parent_id = 77
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    return message


def _payload(
    emoji_id: int | None = _CONFIRM, *, user_id: int = _REACTOR, guild_id: int | None = _GUILD
) -> Any:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=_CHANNEL,
        message_id=_MESSAGE,
        user_id=user_id,
        emoji=discord.PartialEmoji(name="e", id=emoji_id),
        member=None,
    )


def _with_message(bot: Any, message: Any) -> None:
    channel = MagicMock(spec=discord.abc.Messageable)
    channel.fetch_message = AsyncMock(return_value=message)
    bot.get_channel = MagicMock(return_value=channel)


async def _cog(bot: Any, outcome: Outcome | None = None) -> tuple[ReactionActionCog, AsyncMock]:
    caller = AsyncMock(return_value=outcome or Outcome("ok"))
    cog = ReactionActionCog(bot, caller=caller)
    await cog.cog_load()
    return cog, caller


def _serve(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Make the cog's HTTP client answer from `handler`, with no network."""

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(reaction_actions.httpx, "AsyncClient", client)


def _dm_user() -> Any:
    return SimpleNamespace(send=AsyncMock())


def _state(**block: Any) -> Any:
    fields: dict[str, Any] = {
        "kind": "tool_use",
        "id": "t",
        "type": "agent.mcp_tool_use",
        "name": "propose",
        "input": {},
        "mcp_server_name": "tools",
        "status": "complete",
    }
    return SimpleNamespace(content=[ToolUseBlock(**{**fields, **block})])


# --- the mark hook ----------------------------------------------------------------------


async def test_a_finished_call_marks_the_trigger_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature(monkeypatch)
    cog, _ = await _cog(_bot())
    message = _message()
    await mark_trigger_message(message, _state())
    message.add_reaction.assert_awaited_once()
    emoji = message.add_reaction.await_args.args[0]
    assert (emoji.id, emoji.name) == (_MARK, "mark")
    await cog.cog_unload()


@pytest.mark.parametrize(
    "block",
    [
        {"status": "failed"},
        {"is_error": True},
        {"mcp_server_name": "other"},
        {"name": "confirm"},
    ],
)
async def test_a_failed_or_foreign_call_marks_nothing(
    monkeypatch: pytest.MonkeyPatch, block: dict[str, object]
) -> None:
    read = _feature(monkeypatch)
    cog, _ = await _cog(_bot())
    message = _message()
    await mark_trigger_message(message, _state(**block))
    message.add_reaction.assert_not_awaited()
    read.assert_not_awaited()  # no feature read until a binding matched
    await cog.cog_unload()


async def test_the_mark_does_nothing_with_the_feature_off_or_no_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch, "off")
    cog, _ = await _cog(_bot())
    message = _message()
    await mark_trigger_message(message, _state())
    message.add_reaction.assert_not_awaited()
    await cog.cog_unload()
    read = _feature(monkeypatch)
    cog, _ = await _cog(_bot(actions=()))
    await mark_trigger_message(message, _state())
    message.add_reaction.assert_not_awaited()
    read.assert_not_awaited()
    await cog.cog_unload()


async def test_the_mark_skips_direct_messages_and_swallows_a_failed_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    cog, _ = await _cog(_bot())
    dm = _message()
    dm.guild = None
    await mark_trigger_message(dm, _state())
    dm.add_reaction.assert_not_awaited()
    broken = _message()
    broken.add_reaction.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
    await mark_trigger_message(broken, _state())  # does not raise
    await mark_trigger_message(None, _state())
    await cog.cog_unload()


# --- the confirm listener's gates --------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        _payload(user_id=_BOT),
        _payload(emoji_id=333),
        _payload(emoji_id=None),
        _payload(guild_id=None),
        _payload(guild_id=1),
    ],
    ids=["bot_itself", "other_emoji", "unicode_emoji", "dm", "other_server"],
)
async def test_a_reaction_that_is_not_the_confirm_emoji_makes_no_call(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    read = _feature(monkeypatch)
    bot = _bot()
    _with_message(bot, _message(_reaction(_MARK)))
    cog, caller = await _cog(bot)
    await cog.on_raw_reaction_add(payload)
    caller.assert_not_awaited()
    read.assert_not_awaited()  # cheap gates only: no feature read, no fetch
    bot.get_channel.assert_not_called()
    await cog.cog_unload()


async def test_another_bot_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature(monkeypatch)
    bot = _bot()
    _with_message(bot, _message(_reaction(_MARK)))
    cog, caller = await _cog(bot)
    payload = _payload()
    payload.member = SimpleNamespace(bot=True)
    await cog.on_raw_reaction_add(payload)
    caller.assert_not_awaited()
    await cog.cog_unload()


async def test_a_message_without_the_bots_mark_makes_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    for reactions in ([], [_reaction(_MARK, me=False)], [_reaction(333)]):
        bot = _bot()
        _with_message(bot, _message(*reactions))
        cog, caller = await _cog(bot)
        await cog.on_raw_reaction_add(_payload())
        caller.assert_not_awaited()
        await cog.cog_unload()


async def test_the_feature_off_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature(monkeypatch, "off")
    bot = _bot()
    _with_message(bot, _message(_reaction(_MARK)))
    cog, caller = await _cog(bot)
    await cog.on_raw_reaction_add(_payload())
    caller.assert_not_awaited()
    bot.get_channel.assert_not_called()
    await cog.cog_unload()


async def test_with_no_binding_the_listener_does_nothing() -> None:
    bot = _bot(actions=())
    cog, caller = await _cog(bot)
    await cog.on_raw_reaction_add(_payload())
    caller.assert_not_awaited()
    assert reaction_actions._active is None  # pyright: ignore[reportPrivateUsage]


async def test_an_unreadable_message_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature(monkeypatch)
    bot = _bot()
    channel = MagicMock(spec=discord.abc.Messageable)
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    bot.get_channel = MagicMock(return_value=channel)
    cog, caller = await _cog(bot)
    await cog.on_raw_reaction_add(_payload())
    caller.assert_not_awaited()
    await cog.cog_unload()


# --- the call and its outcomes -----------------------------------------------------------


async def test_the_call_names_the_reactor_and_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature(monkeypatch)
    bot = _bot()
    _with_message(bot, _message(_reaction(_MARK)))
    cog, caller = await _cog(bot)
    await cog.on_raw_reaction_add(_payload())
    assert caller.await_args is not None
    (call,) = caller.await_args.args
    assert (call.user, call.guild, call.channel, call.message) == (
        str(_REACTOR),
        str(_GUILD),
        str(_CHANNEL),
        str(_MESSAGE),
    )
    assert (call.server, call.tool, call.agent, call.parent) == ("tools", "confirm", "Helper", None)
    await cog.cog_unload()


async def test_a_thread_names_its_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature(monkeypatch)
    bot = _bot()
    _with_message(bot, _message(_reaction(_MARK), thread=True))
    cog, caller = await _cog(bot)
    await cog.on_raw_reaction_add(_payload())
    assert caller.await_args is not None
    assert caller.await_args.args[0].parent == "77"
    await cog.cog_unload()


async def test_the_post_sends_a_one_minute_credential_to_the_service_not_the_servers_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "attention", "message": "Look"})

    _serve(monkeypatch, handler)
    cog = ReactionActionCog(_bot())
    call = cog._call(_payload(), ACTION, _message())  # pyright: ignore[reportPrivateUsage]
    outcome = await cog._post(call)  # pyright: ignore[reportPrivateUsage]
    assert outcome == Outcome("attention", "Look")
    (request,) = seen
    assert str(request.url) == "https://app.test" + CALL_PATH
    token = request.headers["authorization"].removeprefix("Bearer ")
    claim = verify(token, secret=_SECRET.encode(), now=datetime.now(UTC))
    assert (claim.user, claim.message, claim.tool) == (str(_REACTOR), str(_MESSAGE), "confirm")
    assert json.loads(request.content) == {}


async def test_a_refused_or_unreachable_service_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for status in (403, 500):
        _serve(monkeypatch, lambda _request, status=status: httpx.Response(status, json={}))
        cog = ReactionActionCog(_bot())
        call = cog._call(_payload(), ACTION, _message())  # pyright: ignore[reportPrivateUsage]
        assert (await cog._post(call)).status == "error"  # pyright: ignore[reportPrivateUsage]
    bot = _bot()
    bot.runtime.settings.mcp.app_root_url = None
    cog = ReactionActionCog(bot)
    call = cog._call(_payload(), ACTION, _message())  # pyright: ignore[reportPrivateUsage]
    assert (await cog._post(call)).status == "error"  # pyright: ignore[reportPrivateUsage]


async def test_success_swaps_the_mark_for_the_confirm_emoji(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    bot = _bot()
    message = _message(_reaction(_MARK))
    _with_message(bot, message)
    cog, _ = await _cog(bot, Outcome("ok"))
    await cog.on_raw_reaction_add(_payload())
    assert message.remove_reaction.await_args.args[0].id == _MARK
    assert message.add_reaction.await_args.args[0].id == _CONFIRM
    bot.fetch_user.assert_not_awaited()
    await cog.cog_unload()


async def test_success_clears_an_earlier_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature(monkeypatch)
    bot = _bot()
    warning = SimpleNamespace(emoji=_WARNING, me=True)
    message = _message(_reaction(_MARK), warning)
    _with_message(bot, message)
    cog, _ = await _cog(bot, Outcome("ok"))
    await cog.on_raw_reaction_add(_payload())
    removed = [c.args[0] for c in message.remove_reaction.await_args_list]
    assert _WARNING in removed
    await cog.cog_unload()


@pytest.mark.parametrize(
    "outcome", [Outcome("error", "Not allowed"), Outcome("attention", "It differs")]
)
async def test_error_and_attention_warn_and_send_a_private_card_and_post_nothing(
    monkeypatch: pytest.MonkeyPatch, outcome: Outcome
) -> None:
    _feature(monkeypatch)
    user = _dm_user()
    bot = _bot(dm_user=user)
    message = _message(_reaction(_MARK))
    _with_message(bot, message)
    cog, _ = await _cog(bot, outcome)
    await cog.on_raw_reaction_add(_payload())
    message.add_reaction.assert_awaited_once_with(_WARNING)
    message.remove_reaction.assert_not_awaited()  # the mark stays so the person can retry
    user.send.assert_awaited_once()
    embed = user.send.await_args.kwargs["embed"]
    assert outcome.text in (embed.description or "")
    message.channel.send.assert_not_called()
    message.reply.assert_not_called()
    await cog.cog_unload()


async def test_closed_direct_messages_do_not_fail_the_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    user = _dm_user()
    user.send.side_effect = discord.Forbidden(MagicMock(status=403), "closed")
    bot = _bot(dm_user=user)
    message = _message(_reaction(_MARK))
    _with_message(bot, message)
    cog, _ = await _cog(bot, Outcome("error", "No"))
    await cog.on_raw_reaction_add(_payload())  # does not raise
    message.add_reaction.assert_awaited_once_with(_WARNING)
    await cog.cog_unload()


async def test_a_second_confirm_on_a_busy_message_makes_no_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    bot = _bot()
    _with_message(bot, _message(_reaction(_MARK)))
    cog, caller = await _cog(bot)
    cog._busy.add(_MESSAGE)  # pyright: ignore[reportPrivateUsage]
    await cog.on_raw_reaction_add(_payload())
    caller.assert_not_awaited()
    await cog.cog_unload()


async def test_the_end_of_a_turn_marks_the_trigger_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
    from daimon.core.turn.state import TextBlock, TurnState

    _feature(monkeypatch)
    cog, _ = await _cog(_bot())
    message = _message()
    lifecycle = DiscordTurnLifecycle(
        send=AsyncMock(return_value=MagicMock()),
        edit=AsyncMock(),
        agent_name="test",
        model_id="m",
        trigger_message=message,
    )
    call = _state().content[0]
    await lifecycle.on_terminal_success(
        TurnState(content=[call, TextBlock(kind="text", text="Done.")])
    )
    assert [c.args[0].id for c in message.add_reaction.await_args_list] == [_MARK]
    await cog.cog_unload()
