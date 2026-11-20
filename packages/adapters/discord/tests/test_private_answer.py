"""The Private answer button in a contained room (`audience_hold` mode `on`, E3).

The offer card shows to an internal asker only. A press is checked again and answered
privately: someone else, an outsider, DMs off, a closed DM. A press that passes starts
a new DM turn with the question and tells only the asker "Sent to your DMs". Nothing
private is ever posted in the room. Ids are fictional. Mode `on` is faked because the
registry has no `on` value yet.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import private_answer
from daimon.adapters.discord.bot import (
    _TurnFailureSurface,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.discord.commands import direct_messages
from daimon.adapters.discord.commands.direct_messages import (
    DirectMessageCog,
    _DmRequest,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.discord.private_answer import (
    BUSY,
    DM_CLOSED,
    EXPIRED,
    NOT_ASKER,
    OFFER_AFTER_ANSWER,
    OFFER_AFTER_REFUSAL,
    REFUSALS,
    SENT,
    PrivateAnswerButton,
    PrivateOffer,
    press_private_answer,
    show_private_offer,
)
from daimon.adapters.discord.theme import COLOR_BLURPLE
from daimon.adapters.discord.thread_handoff import HandOverButton
from daimon.adapters.discord.thread_handoff import build_custom_id as handover_id
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_facts import RoomFacts
from daimon.core.audience_refusal import AUDIENCE_CONTAINED
from daimon.core.authz import Subject
from daimon.core.config import DirectMessagePolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.audience_facts import StoredRoom, publish_facts
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from .harness import make_bot
from .test_continuation_dispatch_entry import _make_runtime  # pyright: ignore[reportPrivateUsage]

INTERNAL = "9001"
OTHER_ROLE = "9002"
ASKER = 8001
SOMEONE_ELSE = 8002
ROOM = "7001"
THREAD = "7002"
DM_CHANNEL = 7501
QUESTION = "What did the team decide?"

_MODE: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _mode_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _MODE["value"] = "on"

    async def mode(*_a: object, **_k: object) -> str:
        return _MODE["value"]

    monkeypatch.setattr("daimon.core.channel_audience.audience_mode", mode)
    monkeypatch.setattr("daimon.core.private_answer.audience_mode", mode)


async def _tenant(
    factory: async_sessionmaker[AsyncSession], workspace: str, *, dms: bool = True
) -> uuid.UUID:
    async with factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=workspace)
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=dms)
        await set_access_policy(
            session, tenant_id=tenant.id, policy=TenantAccessPolicy(internal_role_ids=(INTERNAL,))
        )
        now = datetime.now(UTC)
        contained = StoredRoom(RoomFacts(contained=True, narrow=False))
        await publish_facts(
            session,
            tenant_id=tenant.id,
            rooms={ROOM: contained, THREAD: contained},
            now=now,
            fresh_until=now + timedelta(hours=1),
        )
    return tenant.id


def _offer(tenant_id: uuid.UUID, *, mode: str = "on", roles: tuple[str, ...] = (INTERNAL,)):
    return PrivateOffer(
        tenant_id=tenant_id,
        asker_id=ASKER,
        channel_id=ROOM,
        thread_id=THREAD,
        agent="helper",
        question=QUESTION,
        mode=mode,  # type: ignore[arg-type]
        role_ids=roles,
    )


def _world(factory: async_sessionmaker[AsyncSession]) -> tuple[Any, DirectMessageCog]:
    bot = make_bot(_make_runtime(factory))
    cog = DirectMessageCog(bot)
    bot.get_cog = MagicMock(return_value=cog)  # type: ignore[method-assign]
    return bot, cog


def _member(user_id: int, *roles: str) -> Any:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.roles = [SimpleNamespace(id=int(r), is_default=lambda: False) for r in roles]
    dm = MagicMock(spec=discord.DMChannel)
    dm.id = DM_CHANNEL
    dm.send = AsyncMock()
    member.create_dm = AsyncMock(return_value=dm)
    return member


class _Response:
    def __init__(self) -> None:
        self.done = False
        self.sent: list[tuple[discord.Embed, bool]] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, *, embed: discord.Embed, ephemeral: bool) -> None:
        self.sent.append((embed, ephemeral))
        self.done = True

    async def defer(self, *, ephemeral: bool, thinking: bool) -> None:
        assert ephemeral, "a press is answered privately"
        self.done = True


def _interaction(bot: Any, user: Any) -> Any:
    interaction = MagicMock()
    interaction.client = bot
    interaction.user = user
    interaction.id = 990001
    interaction.response = _Response()
    interaction.followup.send = AsyncMock()
    interaction.channel.send = AsyncMock()
    return interaction


def _neutral(embed: discord.Embed) -> bool:
    return embed.color is not None and embed.color.value == COLOR_BLURPLE


def _told(interaction: Any) -> list[tuple[str, bool, bool]]:
    """(text, ephemeral, neutral) of every answer to the presser."""
    sent = [(embed, ephemeral) for embed, ephemeral in interaction.response.sent]
    sent += [
        (c.kwargs["embed"], c.kwargs["ephemeral"]) for c in interaction.followup.send.mock_calls
    ]
    return [(e.description or "", eph, _neutral(e)) for e, eph in sent]


# The offer ------------------------------------------------------------------------------


async def test_the_offer_card_shows_for_an_internal_asker_only_and_never_in_shadow(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Spec check 5: the button shows to an internal asker; shadow logs and shows nothing."""
    tenant_id = await _tenant(db_session_factory, "720000951")
    bot, _cog = _world(db_session_factory)
    send = AsyncMock()

    assert await show_private_offer(bot, _offer(tenant_id), send=send, refused=False)
    assert await show_private_offer(bot, _offer(tenant_id), send=send, refused=True)
    answer_card, view = send.await_args_list[0].args
    assert answer_card.description == OFFER_AFTER_ANSWER and _neutral(answer_card)
    assert send.await_args_list[1].args[0].description == OFFER_AFTER_REFUSAL
    (button,) = view.children
    assert isinstance(button, PrivateAnswerButton)
    assert button.item.label == "Private answer (only me)"
    assert PrivateAnswerButton.__discord_ui_compiled_template__.fullmatch(button.custom_id)

    send.reset_mock()
    assert not await show_private_offer(
        bot, _offer(tenant_id, roles=(OTHER_ROLE,)), send=send, refused=False
    )
    _MODE["value"] = "shadow"
    with capture_logs() as logs:
        assert not await show_private_offer(
            bot, _offer(tenant_id, mode="shadow"), send=send, refused=False
        )
    send.assert_not_awaited()
    offers = [e for e in logs if e.get("kind") == "private_offer"]
    assert [(e["event"], e["channel_id"], e["reason"]) for e in offers] == [
        ("audience.shadow", ROOM, "room_contained")
    ]


# A press that is refused, privately --------------------------------------------------


async def test_a_press_by_someone_else_or_after_expiry_is_refused_privately(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory, "720000952")
    bot, cog = _world(db_session_factory)
    token = cog.private_offers.add(_offer(tenant_id))
    other = _member(SOMEONE_ELSE, INTERNAL)

    by_other = _interaction(bot, other)
    await press_private_answer(by_other, token)
    expired = _interaction(bot, _member(ASKER, INTERNAL))
    await press_private_answer(expired, "0" * 16)

    assert _told(by_other) == [(NOT_ASKER, True, False)]
    assert _told(expired) == [(EXPIRED, True, False)]
    other.create_dm.assert_not_awaited()
    by_other.channel.send.assert_not_awaited()


@pytest.mark.parametrize(
    ("roles", "dms", "expected"),
    [
        ((OTHER_ROLE,), True, REFUSALS["not_internal"]),  # standing is read again at press
        ((INTERNAL,), False, REFUSALS["dms_off"]),
    ],
    ids=["outsider", "dms-off"],
)
async def test_a_press_is_checked_again_and_refused_privately(
    db_session_factory: async_sessionmaker[AsyncSession],
    roles: tuple[str, ...],
    dms: bool,
    expected: str,
) -> None:
    """Spec check 4: an asker who lost the internal role, or a server with DMs off."""
    tenant_id = await _tenant(db_session_factory, f"72000095{3 + dms}", dms=dms)
    bot, cog = _world(db_session_factory)
    token = cog.private_offers.add(_offer(tenant_id))
    asker = _member(ASKER, *roles)
    interaction = _interaction(bot, asker)

    await press_private_answer(interaction, token)

    assert _told(interaction) == [(expected, True, False)]
    asker.create_dm.assert_not_awaited()
    interaction.channel.send.assert_not_awaited()


def _admitted(monkeypatch: pytest.MonkeyPatch, cog: DirectMessageCog) -> dict[str, AsyncMock]:
    """The cog's DM path with the core calls faked: admission, the start and the turn."""
    cog._subject = AsyncMock(  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
        return_value=Subject(
            is_admin=False,
            platform_user_id=str(ASKER),
            verified_membership=True,
            role_ids=(INTERNAL,),
        )
    )
    fakes = {
        "get_conversation": AsyncMock(return_value=SimpleNamespace(scope_id="dm:existing")),
        "admit_cold_dm": AsyncMock(
            return_value=SimpleNamespace(config=SimpleNamespace(agent_name="helper"))
        ),
        "start_cold_dm": AsyncMock(return_value=SimpleNamespace(scope_id="dm:private")),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(direct_messages, name, fake)
    monkeypatch.setattr(direct_messages, "tenant_ready", lambda *_a, **_k: True)
    fakes["turn"] = AsyncMock()
    cog._turn = fakes["turn"]  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    return fakes


@pytest.mark.parametrize("closed", ["create_dm", "send"])
async def test_a_closed_dm_says_so_privately_and_posts_nothing_in_the_room(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    closed: str,
) -> None:
    tenant_id = await _tenant(db_session_factory, f"72000096{len(closed)}")
    bot, cog = _world(db_session_factory)
    fakes = _admitted(monkeypatch, cog)
    token = cog.private_offers.add(_offer(tenant_id))
    asker = _member(ASKER, INTERNAL)
    forbidden = discord.Forbidden(MagicMock(status=403), "Cannot send messages to this user")
    if closed == "create_dm":
        asker.create_dm.side_effect = forbidden
    else:
        asker.create_dm.return_value.send.side_effect = forbidden
    interaction = _interaction(bot, asker)

    await press_private_answer(interaction, token)

    assert _told(interaction) == [(DM_CLOSED, True, False)]
    fakes["start_cold_dm"].assert_not_awaited()
    fakes["turn"].assert_not_awaited()
    interaction.channel.send.assert_not_awaited()


async def test_a_press_while_the_dm_is_answering_is_refused_as_busy(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await _tenant(db_session_factory, "720000971")
    bot, cog = _world(db_session_factory)
    fakes = _admitted(monkeypatch, cog)
    token = cog.private_offers.add(_offer(tenant_id))
    interaction = _interaction(bot, _member(ASKER, INTERNAL))

    async with cog._lock((ASKER, DM_CHANNEL)):  # pyright: ignore[reportPrivateUsage]
        await press_private_answer(interaction, token)

    assert _told(interaction) == [(BUSY, True, False)]
    fakes["start_cold_dm"].assert_not_awaited()


# A press that starts the Private answer -------------------------------------------------


async def test_a_press_during_an_existing_dm_conversation_starts_a_fresh_private_turn(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await _tenant(db_session_factory, "720000972")
    bot, cog = _world(db_session_factory)
    fakes = _admitted(monkeypatch, cog)
    order: list[str] = []
    fakes["turn"].side_effect = lambda *_a: order.append("turn")
    token = cog.private_offers.add(_offer(tenant_id))
    asker = _member(ASKER, INTERNAL)
    interaction = _interaction(bot, asker)
    interaction.followup.send.side_effect = lambda **_k: order.append("told")

    await press_private_answer(interaction, token)

    assert _told(interaction) == [(SENT, True, True)], "only the asker is told, privately"
    assert order == ["told", "turn"], "the asker is told before the turn runs"
    admitted = fakes["admit_cold_dm"].await_args.kwargs
    assert (admitted["dm_no_source"], admitted["agent_name"]) == (True, "helper")
    assert admitted["requester_org"] is RequesterOrg.HOST
    started = fakes["start_cold_dm"].await_args.kwargs
    assert started["private_answer"] is True
    assert started["expected_scope_id"] == "dm:existing", "it replaces the current DM"
    request, conversation, batch = fakes["turn"].await_args.args
    assert isinstance(request, _DmRequest) and request.content == QUESTION
    assert conversation.scope_id == "dm:private" and batch == ()
    dm = asker.create_dm.return_value
    assert _neutral(dm.send.await_args.kwargs["embed"])
    interaction.channel.send.assert_not_awaited()


async def test_the_private_turn_asks_the_question_with_no_room_history(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stand-in request carries the question; there is nothing else to read."""
    tenant_id = await _tenant(db_session_factory, "720000973")
    _bot, cog = _world(db_session_factory)
    _admitted(monkeypatch, cog)
    del cog._turn  # pyright: ignore[reportPrivateUsage]  # the real turn this time
    ticket = MagicMock()
    ticket.wait = AsyncMock(return_value="started")
    cog.bot.turn_queue.admit = MagicMock(return_value=ticket)  # type: ignore[method-assign]
    monkeypatch.setattr(direct_messages, "env_dm_policy", lambda *_a, **_k: DirectMessagePolicy())
    reply = AsyncMock(return_value=None)
    monkeypatch.setattr(direct_messages, "reply_to_dm_result", reply)
    dm = _member(ASKER, INTERNAL).create_dm.return_value
    dm.typing = MagicMock(return_value=AsyncMock())
    request = _DmRequest(id=990001, author=_member(ASKER, INTERNAL), channel=dm, content=QUESTION)

    await cog._turn(  # pyright: ignore[reportPrivateUsage]
        request, SimpleNamespace(tenant_id=tenant_id, scope_id="dm:private"), ()
    )

    kwargs = reply.await_args.kwargs
    assert (kwargs["text"], kwargs["prefix"], kwargs["message_id"]) == (QUESTION, "", "990001")


# The room's turn ------------------------------------------------------------------------


async def test_a_refused_answer_is_followed_by_the_offer_only_when_one_was_set(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate A refused the answer (the room became contained mid-turn): the error card,
    then the offer. Mode `off` sets no offer, so the error alone is shown, as today."""
    tenant_id = await _tenant(db_session_factory, "720000974")
    bot, _cog = _world(db_session_factory)
    shown = AsyncMock(return_value=True)
    monkeypatch.setattr(private_answer, "show_private_offer", shown)
    rendered = AsyncMock()
    monkeypatch.setattr(bot, "_render_turn_error", rendered)
    message = MagicMock(spec=discord.Message)
    message.id = 9100
    message.channel = MagicMock(spec=discord.TextChannel)
    message.channel.id = int(ROOM)
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()

    async def refused_turn(*_a: object, failure_surface: _TurnFailureSurface, **_k: object) -> None:
        failure_surface.private_offer = offer
        raise AdmissionDenied(reason="invoker_not_allowed", detail=AUDIENCE_CONTAINED)

    monkeypatch.setattr(bot, "_orchestrate", refused_turn)
    for offer in (_offer(tenant_id), None):  # noqa: B007  # read by refused_turn
        await bot._handle_mention(message, "720000974", tenant_id)  # pyright: ignore[reportPrivateUsage]

    assert rendered.await_count == 2, "the refusal is rendered as today"
    assert shown.await_count == 1
    assert shown.await_args.kwargs["refused"] is True
    assert shown.await_args.args[1].thread_id is None, "no thread opened: the channel"


async def test_an_answer_gets_the_offer_through_the_turns_sender(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await _tenant(db_session_factory, "720000975")
    bot, _cog = _world(db_session_factory)
    shown = AsyncMock(return_value=True)
    monkeypatch.setattr(private_answer, "show_private_offer", shown)
    thread = MagicMock(spec=discord.Thread)
    thread.id = int(THREAD)
    turn_send = AsyncMock()
    surface = _TurnFailureSurface(private_offer=_offer(tenant_id), audience_refused=lambda: False)

    await bot._offer_after_answer(  # pyright: ignore[reportPrivateUsage]
        surface, thread=thread, answered=False, send=turn_send
    )
    shown.assert_not_awaited()  # no answer and no refusal: nothing to offer
    await bot._offer_after_answer(  # pyright: ignore[reportPrivateUsage]
        surface, thread=thread, answered=True, send=turn_send
    )

    assert shown.await_args.kwargs["refused"] is False
    assert shown.await_args.args[1].thread_id == THREAD
    card = discord.Embed(description="x")
    await shown.await_args.kwargs["send"](card, discord.ui.View())
    assert turn_send.await_args.kwargs["embed"] is card


def test_the_button_template_overlaps_no_other_button() -> None:
    ours = PrivateAnswerButton(token="ab" * 8).item.custom_id
    assert ours is not None
    assert HandOverButton.__discord_ui_compiled_template__.fullmatch(ours) is None
    assert PrivateAnswerButton.__discord_ui_compiled_template__.fullmatch(handover_id("ag")) is None
