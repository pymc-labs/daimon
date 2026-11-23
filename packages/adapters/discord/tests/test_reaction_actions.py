"""Reaction actions on Discord: the mark hook, the confirm listener's gates and its outcomes."""

from __future__ import annotations

import asyncio
import json
import time
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
from daimon.core.mcp_identity import relay_url
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
_PUBLIC = "https://app.test"
_RELAY = relay_url(_PUBLIC, "tools")

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
    settings.mcp.app_root_url = _PUBLIC
    settings.mcp.public_url = _PUBLIC
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


def _agent(name: str = "Helper", label: str = "tools", url: str = _RELAY) -> Any:
    return SimpleNamespace(name=name, mcp_servers=[SimpleNamespace(name=label, url=url)])


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
    await mark_trigger_message(message, _state(), _agent())
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
    await mark_trigger_message(message, _state(**block), _agent())
    message.add_reaction.assert_not_awaited()
    read.assert_not_awaited()  # no feature read until a binding matched
    await cog.cog_unload()


@pytest.mark.parametrize(
    "agent",
    [
        None,
        _agent(name="Intruder"),
        _agent(url="https://evil.example/mcp-identity/tools"),
        _agent(url=relay_url(_PUBLIC, "other")),
        _agent(url="https://tools.example/mcp"),
        _agent(label="renamed"),
    ],
    ids=["no_agent", "other_agent", "other_host", "other_relay", "not_relay", "label_not_attached"],
)
async def test_the_mark_needs_the_bound_agent_and_the_bound_relay_url(
    monkeypatch: pytest.MonkeyPatch, agent: Any
) -> None:
    read = _feature(monkeypatch)
    cog, _ = await _cog(_bot())
    message = _message()
    await mark_trigger_message(message, _state(), agent)
    message.add_reaction.assert_not_awaited()
    read.assert_not_awaited()
    await cog.cog_unload()


async def test_the_label_may_differ_when_the_url_is_the_bound_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    cog, _ = await _cog(_bot())
    message = _message()
    await mark_trigger_message(message, _state(mcp_server_name="mine"), _agent(label="mine"))
    message.add_reaction.assert_awaited_once()
    await cog.cog_unload()


async def test_the_mark_does_nothing_with_the_feature_off_or_no_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch, "off")
    cog, _ = await _cog(_bot())
    message = _message()
    await mark_trigger_message(message, _state(), _agent())
    message.add_reaction.assert_not_awaited()
    await cog.cog_unload()
    read = _feature(monkeypatch)
    cog, _ = await _cog(_bot(actions=()))
    await mark_trigger_message(message, _state(), _agent())
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
    await mark_trigger_message(dm, _state(), _agent())
    dm.add_reaction.assert_not_awaited()
    broken = _message()
    broken.add_reaction.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
    await mark_trigger_message(broken, _state(), _agent())  # does not raise
    await mark_trigger_message(None, _state(), _agent())
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


async def test_the_post_sends_a_short_credential_to_the_service_not_the_servers_key(
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


def _live_message() -> Any:
    """A message whose reactions change as the cog writes, starting from the real state:
    the bot's own mark emoji and the person's confirm emoji."""
    message = _message(_reaction(_MARK), _reaction(_CONFIRM, me=False))

    async def add(emoji: Any) -> None:
        message.reactions.append(
            SimpleNamespace(emoji=emoji, me=True, id=getattr(emoji, "id", None))
        )

    async def remove(emoji: Any, member: Any) -> None:
        ident = getattr(emoji, "id", None)
        mine = member.id == _BOT
        message.reactions[:] = [
            r
            for r in message.reactions
            if not (getattr(r.emoji, "id", None) == ident and r.emoji == emoji and r.me == mine)
        ]

    message.add_reaction = AsyncMock(side_effect=add)
    message.remove_reaction = AsyncMock(side_effect=remove)
    return message


@pytest.mark.parametrize(
    "outcome", [Outcome("error", "Not allowed"), Outcome("attention", "It differs")]
)
async def test_error_and_attention_warn_remove_the_persons_emoji_and_card_the_person(
    monkeypatch: pytest.MonkeyPatch, outcome: Outcome
) -> None:
    _feature(monkeypatch)
    user = _dm_user()
    bot = _bot(dm_user=user)
    message = _message(_reaction(_MARK), _reaction(_CONFIRM, me=False))
    _with_message(bot, message)
    cog, _ = await _cog(bot, outcome)
    await cog.on_raw_reaction_add(_payload())
    removed = message.remove_reaction.await_args_list
    assert [(c.args[0].id, c.args[1].id) for c in removed] == [(_CONFIRM, _REACTOR)]
    message.add_reaction.assert_awaited_once_with(_WARNING)  # the mark stays: a retry is possible
    user.send.assert_awaited_once()
    text = user.send.await_args.kwargs["embed"].description or ""
    assert outcome.text in text
    assert "add the emoji again" in text
    assert "remove your emoji" not in text
    message.channel.send.assert_not_called()
    message.reply.assert_not_called()
    await cog.cog_unload()


async def test_the_card_says_to_remove_the_emoji_when_the_bot_could_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    user = _dm_user()
    bot = _bot(dm_user=user)
    message = _message(_reaction(_MARK), _reaction(_CONFIRM, me=False))
    message.remove_reaction.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    _with_message(bot, message)
    cog, _ = await _cog(bot, Outcome("error", "No"))
    await cog.on_raw_reaction_add(_payload())  # does not raise
    message.add_reaction.assert_awaited_once_with(_WARNING)
    assert "remove your emoji and add it again" in user.send.await_args.kwargs["embed"].description
    await cog.cog_unload()


async def test_closed_direct_messages_do_not_fail_the_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    user = _dm_user()
    user.send.side_effect = discord.Forbidden(MagicMock(status=403), "closed")
    bot = _bot(dm_user=user)
    message = _message(_reaction(_MARK), _reaction(_CONFIRM, me=False))
    _with_message(bot, message)
    cog, _ = await _cog(bot, Outcome("error", "No"))
    await cog.on_raw_reaction_add(_payload())  # does not raise
    message.add_reaction.assert_awaited_once_with(_WARNING)
    await cog.cog_unload()


async def test_a_failure_that_shows_nowhere_is_logged_with_the_message_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    user = _dm_user()
    user.send.side_effect = discord.Forbidden(MagicMock(status=403), "closed")
    bot = _bot(dm_user=user)
    message = _message(_reaction(_MARK), _reaction(_CONFIRM, me=False))
    message.add_reaction.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    _with_message(bot, message)
    warned = MagicMock()
    monkeypatch.setattr(reaction_actions.log, "warning", warned)
    cog, _ = await _cog(bot, Outcome("error", "No"))
    await cog.on_raw_reaction_add(_payload())
    warned.assert_called_once_with(
        "reaction_action.failure_unshown",
        message_url=f"https://discord.com/channels/{_GUILD}/{_CHANNEL}/{_MESSAGE}",
    )
    await cog.cog_unload()


async def test_a_second_person_after_a_success_makes_no_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    bot = _bot()
    message = _live_message()
    _with_message(bot, message)
    gate = asyncio.Event()
    calls: list[str] = []

    async def caller(call: Any) -> Outcome:
        calls.append(call.user)
        await gate.wait()
        return Outcome("ok")

    cog = ReactionActionCog(bot, caller=caller)
    await cog.cog_load()
    alice = asyncio.create_task(cog.on_raw_reaction_add(_payload(user_id=1)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    bob = asyncio.create_task(cog.on_raw_reaction_add(_payload(user_id=2)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == ["1"]  # Bob waits his turn, he is not dropped and does not run alongside
    gate.set()
    await asyncio.gather(alice, bob)
    assert calls == ["1"]  # the mark is gone after the success, so Bob makes no call
    assert [r.emoji.id for r in message.reactions if r.me] == [_CONFIRM]
    assert cog._locks == {}  # pyright: ignore[reportPrivateUsage]
    await cog.cog_unload()


async def test_a_second_person_after_a_failure_gets_their_own_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    bot = _bot(dm_user=_dm_user())
    message = _live_message()
    _with_message(bot, message)
    gate = asyncio.Event()
    calls: list[str] = []

    async def caller(call: Any) -> Outcome:
        calls.append(call.user)
        await gate.wait()
        return Outcome("error", "No") if call.user == "1" else Outcome("ok")

    cog = ReactionActionCog(bot, caller=caller)
    await cog.cog_load()
    alice = asyncio.create_task(cog.on_raw_reaction_add(_payload(user_id=1)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    bob = asyncio.create_task(cog.on_raw_reaction_add(_payload(user_id=2)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == ["1"]
    gate.set()
    await asyncio.gather(alice, bob)
    assert calls == ["1", "2"]  # the mark stayed after Alice's failure, so Bob's call ran
    assert [r.emoji.id for r in message.reactions if r.me and r.emoji.id] == [_CONFIRM]
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
        turn_agent=_agent(),
    )
    call = _state().content[0]
    await lifecycle.on_terminal_success(
        TurnState(content=[call, TextBlock(kind="text", text="Done.")])
    )
    assert [c.args[0].id for c in message.add_reaction.await_args_list] == [_MARK]
    await cog.cog_unload()


async def test_a_success_stays_done_when_the_mark_cannot_be_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(monkeypatch)
    bot = _bot()
    message = _live_message()
    message.remove_reaction.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    _with_message(bot, message)
    gate = asyncio.Event()
    calls: list[str] = []

    async def caller(call: Any) -> Outcome:
        calls.append(call.user)
        await gate.wait()
        return Outcome("ok")

    cog = ReactionActionCog(bot, caller=caller)
    await cog.cog_load()
    alice = asyncio.create_task(cog.on_raw_reaction_add(_payload(user_id=1)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    bob = asyncio.create_task(cog.on_raw_reaction_add(_payload(user_id=2)))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(alice, bob)
    assert [r.emoji.id for r in message.reactions if r.me] == [_MARK]  # the mark is still there
    assert calls == ["1"]
    await cog.on_raw_reaction_add(_payload(user_id=3))  # a later reaction does nothing either
    assert calls == ["1"]
    await cog.cog_unload()


async def test_the_memory_of_successes_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = ReactionActionCog(_bot())
    for message_id in range(reaction_actions._DONE_MAX + 5):  # pyright: ignore[reportPrivateUsage]
        cog._remember(message_id, ACTION)  # pyright: ignore[reportPrivateUsage]
    assert len(cog._done) == reaction_actions._DONE_MAX  # pyright: ignore[reportPrivateUsage]
    assert not cog._was_done(0, ACTION)  # pyright: ignore[reportPrivateUsage]
    assert cog._was_done(reaction_actions._DONE_MAX + 4, ACTION)  # pyright: ignore[reportPrivateUsage]
    later = time.monotonic() + reaction_actions._DONE_SECONDS + 1  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(reaction_actions.time, "monotonic", lambda: later)
    assert not cog._was_done(reaction_actions._DONE_MAX + 4, ACTION)  # pyright: ignore[reportPrivateUsage]
