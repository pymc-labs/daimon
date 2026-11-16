"""Imported timers that fire in a plain channel: access proof, delivery, and the wake path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from daimon.adapters.discord import timer_delivery
from daimon.adapters.discord.timer_delivery import (
    ChannelAccess,
    deliver_channel_timer,
    require_channel_timer_access,
)
from daimon.core.access_policy import CHANNEL_SCOPE_PREFIX
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.scope_timers import DeliveryFailed, DeliveryRetry
from daimon.core.continuity.wakes import enqueue_wake
from daimon.core.direct_messages import DirectMessageAccessRevoked, DmRoutineReply
from daimon.core.stores.domain import Role, TaskContinuationRow
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot
from .test_continuation_dispatch_entry import _make_runtime  # pyright: ignore[reportPrivateUsage]
from .test_dm_timer_delivery import _poll, _row  # pyright: ignore[reportPrivateUsage]

_GUILD = 710000099
_CHANNEL = 4241
_OWNER = 555
_BOT = 999


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timer_delivery, "DELIVERY_BACKOFF", (0.0, 0.0))


def _http_error(status: int = 503) -> discord.HTTPException:
    return discord.HTTPException(MagicMock(status=status, reason="x"), "boom")


def _denied(kind: type[discord.HTTPException] = discord.NotFound) -> discord.HTTPException:
    return kind(MagicMock(status=404, reason="gone"), "gone")


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str = str(_GUILD),
    channel_id: str = str(_CHANNEL),
) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=workspace_id)
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id=channel_id,
        thread_id=f"{CHANNEL_SCOPE_PREFIX}{uuid.uuid4()}",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id=str(_OWNER),
        target_ma_agent_id="ag_target",
        target_name="target-agent",
        requested_work="stretch",
        reason="timer",
        idempotency_key=uuid.uuid4(),
    )
    await enqueue_wake(sessionmaker, request, available_at=datetime.now(UTC) - timedelta(seconds=1))
    return tenant.id, request.idempotency_key


class Rest:
    """REST payloads for one guild; the owner and the bot hold different roles."""

    def __init__(self, client: discord.Client) -> None:
        self.owner_perms = discord.Permissions(view_channel=True, send_messages=True)
        self.bot_perms = discord.Permissions(view_channel=True, send_messages=True)
        self.channel_type = 0
        self.member_error: Exception | None = None
        self.channel_error: Exception | None = None
        self.owner_is_bot = False
        client._connection.user = SimpleNamespace(id=_BOT)  # pyright: ignore[reportAttributeAccessIssue]
        http = client.http
        http.get_guild = AsyncMock(side_effect=self.guild)
        http.get_roles = AsyncMock(side_effect=self.roles)
        http.get_member = AsyncMock(side_effect=self.member)
        http.get_channel = AsyncMock(side_effect=self.channel)

    def guild(self, guild_id: int, **_kwargs: object) -> dict[str, object]:
        return {"id": str(guild_id), "name": "g", "owner_id": "1", "roles": []}

    def roles(self, _guild_id: int) -> list[dict[str, object]]:
        return [
            {"id": str(_GUILD), "name": "@everyone", "permissions": "0"},
            {"id": "77", "name": "owner", "permissions": str(self.owner_perms.value)},
            {"id": "88", "name": "bot", "permissions": str(self.bot_perms.value)},
        ]

    def member(self, _guild_id: int, user_id: int) -> dict[str, object]:
        if self.member_error is not None:
            raise self.member_error
        is_owner = user_id == _OWNER
        return {
            "user": {
                "id": str(user_id),
                "username": "u",
                "discriminator": "0",
                "avatar": None,
                "bot": self.owner_is_bot if is_owner else True,
            },
            "roles": ["77" if is_owner else "88"],
            "flags": 0,
        }

    def channel(self, channel_id: int) -> dict[str, object]:
        if self.channel_error is not None:
            raise self.channel_error
        assert channel_id == _CHANNEL
        return {
            "id": str(_CHANNEL),
            "guild_id": str(_GUILD),
            "type": self.channel_type,
            "name": "room",
            "parent_id": "300",
            "position": 0,
            "permission_overwrites": [],
            "bitrate": 64000,
            "user_limit": 0,
        }


async def _access_row(sessionmaker: async_sessionmaker[AsyncSession]) -> TaskContinuationRow:
    _, key = await _seed(sessionmaker)
    return await _row(sessionmaker, key)


async def test_the_owner_and_the_bot_still_in_the_channel_are_proved_over_rest(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _access_row(db_session_factory)
    client = discord.Client(intents=discord.Intents.none())
    rest = Rest(client)

    access = await require_channel_timer_access(db_session_factory, row, client)

    assert access.member.id == _OWNER and access.category_id == "300"
    rest.http_guild = client.http.get_guild.assert_awaited_once_with(_GUILD, with_counts=False)


@pytest.mark.parametrize(
    "case",
    [
        "owner_cannot_view",
        "bot_cannot_view",
        "bot_cannot_send",
        "owner_left",
        "owner_is_a_bot",
        "channel_gone",
        "channel_is_a_voice_room",
        "channel_closed_by_rules",
    ],
)
async def test_a_lost_channel_stops_the_timer_without_a_message(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    row = await _access_row(db_session_factory)
    client = discord.Client(intents=discord.Intents.none())
    rest = Rest(client)
    if case == "owner_cannot_view":
        rest.owner_perms = discord.Permissions(send_messages=True)
    if case == "bot_cannot_view":
        rest.bot_perms = discord.Permissions(send_messages=True)
    if case == "bot_cannot_send":
        rest.bot_perms = discord.Permissions(view_channel=True)
    if case == "owner_left":
        rest.member_error = _denied()
    if case == "owner_is_a_bot":
        rest.owner_is_bot = True
    if case == "channel_gone":
        rest.channel_error = _denied(discord.Forbidden)
    if case == "channel_is_a_voice_room":
        rest.channel_type = 2
    if case == "channel_closed_by_rules":
        monkeypatch.setattr(
            timer_delivery,
            "protection_state",
            AsyncMock(return_value=SimpleNamespace(may_post=False)),
        )

    with pytest.raises(AdmissionDenied) as caught:
        await require_channel_timer_access(db_session_factory, row, client)

    assert caught.value.reason == "invoker_not_allowed"


async def test_a_transient_discord_error_is_not_a_lost_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _access_row(db_session_factory)
    client = discord.Client(intents=discord.Intents.none())
    rest = Rest(client)
    rest.channel_error = _http_error(503)

    with pytest.raises(discord.HTTPException) as caught:
        await require_channel_timer_access(db_session_factory, row, client)

    assert not isinstance(caught.value, discord.Forbidden | discord.NotFound)


# deliver_channel_timer -----------------------------------------------------------


def _access() -> ChannelAccess:
    return ChannelAccess(member=MagicMock(spec=discord.Member), category_id=None)


class Delivery:
    def __init__(self, *, text: str = "hello") -> None:
        self.row = SimpleNamespace(id=uuid.uuid4(), parent_channel_id=str(_CHANNEL))
        self.check = AsyncMock(return_value=_access())
        self.turn = AsyncMock(return_value=text)
        self.channel = MagicMock()
        self.channel.send = AsyncMock()

    async def run(self) -> int:
        return await deliver_channel_timer(
            self.row,  # pyright: ignore[reportArgumentType]
            check_access=self.check,
            run_turn=self.turn,
            channel_for=lambda _id: self.channel,
        )


async def test_the_reply_is_posted_in_the_channel_in_chunks_with_mentions_off() -> None:
    delivery = Delivery(text="word " * 900)

    sent = await delivery.run()

    assert sent == 3 and delivery.channel.send.await_count == 3
    for call in delivery.channel.send.await_args_list:
        assert len(call.kwargs["content"]) <= 2000
        assert call.kwargs["allowed_mentions"].everyone is False
        assert call.kwargs["allowed_mentions"].users is False
    assert delivery.check.await_count == 1 + 1 + 2, "before, after the turn, between chunks"


async def test_no_turn_runs_when_the_channel_is_already_closed() -> None:
    delivery = Delivery()
    delivery.check.side_effect = AdmissionDenied(reason="invoker_not_allowed")

    with pytest.raises(AdmissionDenied):
        await delivery.run()

    delivery.turn.assert_not_awaited()
    delivery.channel.send.assert_not_awaited()


async def test_a_channel_closed_during_the_turn_gets_no_message() -> None:
    delivery = Delivery()
    delivery.check.side_effect = [_access(), AdmissionDenied(reason="invoker_not_allowed")]

    with pytest.raises(AdmissionDenied):
        await delivery.run()

    delivery.turn.assert_awaited_once()
    delivery.channel.send.assert_not_awaited()


@pytest.mark.parametrize("error", [_http_error(503), _http_error(429), aiohttp.ClientError()])
async def test_a_transient_error_before_the_turn_puts_the_timer_back(error: Exception) -> None:
    delivery = Delivery()
    delivery.check.side_effect = error

    with pytest.raises(DeliveryRetry):
        await delivery.run()

    delivery.turn.assert_not_awaited()


async def test_any_other_4xx_before_the_turn_is_final() -> None:
    delivery = Delivery()
    delivery.check.side_effect = _http_error(400)

    with pytest.raises(discord.HTTPException):
        await delivery.run()

    delivery.turn.assert_not_awaited()


async def test_a_transient_send_error_is_retried_in_place_without_rerunning_the_turn() -> None:
    delivery = Delivery()
    delivery.channel.send.side_effect = [_http_error(503), None]

    assert await delivery.run() == 1

    assert delivery.channel.send.await_count == 2
    delivery.turn.assert_awaited_once()


async def test_a_send_that_keeps_failing_settles_as_a_delivery_failure() -> None:
    delivery = Delivery()
    delivery.channel.send.side_effect = _http_error(503)

    with pytest.raises(DeliveryFailed):
        await delivery.run()

    delivery.turn.assert_awaited_once()


async def test_a_channel_that_refuses_the_post_settles_as_not_allowed() -> None:
    delivery = Delivery()
    delivery.channel.send.side_effect = _denied(discord.Forbidden)

    with pytest.raises(AdmissionDenied) as caught:
        await delivery.run()

    assert caught.value.reason == "invoker_not_allowed"


async def test_a_lookup_error_while_sending_is_not_read_as_a_closed_place() -> None:
    """Only opening the place may say it is gone; a send bug must not settle the timer as closed."""
    delivery = Delivery()
    delivery.channel.send.side_effect = LookupError("not a place error")

    with pytest.raises(LookupError):
        await delivery.run()

    delivery.turn.assert_awaited_once()


async def test_a_lookup_error_while_opening_the_place_settles_as_not_allowed() -> None:
    """Opening the place raising `LookupError` means it is gone, so the timer settles."""
    delivery = Delivery()

    def gone(_id: int) -> discord.abc.Messageable:
        raise LookupError("gone")

    with pytest.raises(AdmissionDenied) as caught:
        await deliver_channel_timer(
            delivery.row,  # pyright: ignore[reportArgumentType]
            check_access=delivery.check,
            run_turn=delivery.turn,
            channel_for=gone,
        )

    assert caught.value.reason == "invoker_not_allowed"


# The wake path -------------------------------------------------------------------


def _channel_bot(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    access: object | None = None,
    reply: str = "done",
    refuse: str | None = None,
    send: AsyncMock | None = None,
):
    bot = make_bot(_make_runtime(sessionmaker))
    member = MagicMock(spec=discord.Member)
    member.id = _OWNER
    member.guild = MagicMock(owner_id=1)
    member.roles = []
    member.guild_permissions = discord.Permissions.none()
    seen = ChannelAccess(member=member, category_id="300")
    monkeypatch.setattr(
        timer_delivery,
        "require_channel_timer_access",
        access or AsyncMock(return_value=seen),
    )
    turn = AsyncMock(
        side_effect=DirectMessageAccessRevoked(refuse, message="no") if refuse else None,
        return_value=DmRoutineReply(text=reply, session_id="s", producing_agent_id="ag_target"),
    )
    monkeypatch.setattr("daimon.core.direct_messages.run_channel_timer", turn)
    channel = MagicMock()
    channel.send = send or AsyncMock()
    bot.get_partial_messageable = MagicMock(return_value=channel)  # pyright: ignore[reportAttributeAccessIssue]
    return bot, turn, channel


async def test_a_channel_timer_runs_as_its_owner_and_posts_in_its_channel(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, key = await _seed(db_session_factory)
    bot, turn, channel = _channel_bot(db_session_factory, monkeypatch, reply="stretch now")

    await _poll(db_session_factory, bot)

    turn.assert_awaited_once()
    kwargs = turn.await_args.kwargs
    assert "stretch" in kwargs["prompt"] and kwargs["role"] is Role.USER
    assert kwargs["category_id"] == "300"
    bot.get_partial_messageable.assert_called_with(_CHANNEL)
    assert channel.send.await_args.kwargs["content"] == "stretch now"
    assert (await _row(db_session_factory, key)).status == "delivered"
    await _poll(db_session_factory, bot)
    turn.assert_awaited_once()


async def test_a_refused_channel_timer_settles_with_a_reason_and_posts_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, key = await _seed(db_session_factory)
    bot, _turn, channel = _channel_bot(
        db_session_factory, monkeypatch, refuse="invoker_not_allowed"
    )

    await _poll(db_session_factory, bot)

    row = await _row(db_session_factory, key)
    assert (row.status, row.skip_reason) == ("skipped", "admission_denied:invoker_not_allowed")
    channel.send.assert_not_awaited()


async def test_a_channel_timer_whose_access_is_gone_never_runs(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, key = await _seed(db_session_factory)
    bot, turn, channel = _channel_bot(
        db_session_factory,
        monkeypatch,
        access=AsyncMock(side_effect=AdmissionDenied(reason="invoker_not_allowed")),
    )

    await _poll(db_session_factory, bot)

    row = await _row(db_session_factory, key)
    assert (row.status, row.skip_reason) == ("skipped", "admission_denied:invoker_not_allowed")
    turn.assert_not_awaited()
    channel.send.assert_not_awaited()


async def test_a_transient_error_before_a_channel_turn_retries_then_settles(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, key = await _seed(db_session_factory)
    bot, turn, _channel = _channel_bot(
        db_session_factory, monkeypatch, access=AsyncMock(side_effect=_http_error(503))
    )

    await _poll(db_session_factory, bot)

    row = await _row(db_session_factory, key)
    assert row.status == "pending" and row.available_at is not None
    turn.assert_not_awaited()
