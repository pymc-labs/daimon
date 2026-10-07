"""Tests for FeedbackReactionCog.on_raw_reaction_add -- gates, author
verification, and vote persistence.

Rows are verified through a spy wrapped around the real `record_vote` store
call (see `_spy_on_record_vote`), never a raw ORM re-read: the private ORM
module is banned in this test tree (T9, `scripts/lint_anti_patterns.sh`),
and re-calling `record_vote` a second time to "read back" a row would
itself overwrite fields (`account_id`, `ma_session_id`) the listener just
set. The spy captures the exact `RecordVoteResult` the listener wrote (or
an empty list, for every "writes nothing" assertion) with zero extra I/O.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import feedback_reactions
from daimon.adapters.discord.feedback_reactions import FeedbackReactionCog
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.core.message_feedback import CUSTOM_ID_PATTERN
from daimon.core.stores.domain import RecordVoteResult
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_BOT_USER_ID = 999999999999999999
_REACTOR_ID = 100000000000000001
_OTHER_REACTOR_ID = 100000000000000002
_GUILD_ID = 555555555555555555


async def test_application_webhook_post_counts_as_bot_authored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    message.author.id = 900
    message.author.name = "Research"
    message.webhook_id = 900
    message.application_id = _BOT_USER_ID
    channel.fetch_message = AsyncMock(return_value=message)
    bot = _fake_bot(sessionmaker=MagicMock(), get_channel=MagicMock(return_value=channel))
    owns = AsyncMock(return_value=True)
    monkeypatch.setattr(DiscordPostTransport, "owns_message", owns)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=123,
        channel_id=456,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="👍",
        message_author_id=900,
    )
    assert await cog._is_bot_message_via_fetch(payload, bot_user_id=_BOT_USER_ID)  # pyright: ignore[reportPrivateUsage]
    owns.assert_awaited_once()


async def test_known_human_author_never_fetches_message() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock()
    bot = _fake_bot(sessionmaker=MagicMock(), get_channel=MagicMock(return_value=channel))
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=123,
        channel_id=456,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="👍",
        message_author_id=1234,
    )
    await cog._record_vote_from_reaction(payload)  # pyright: ignore[reportPrivateUsage]
    channel.fetch_message.assert_not_awaited()


async def test_webhook_after_restart_is_discovered_without_application_id() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 456
    message = MagicMock(spec=discord.Message)
    message.author.id = 900
    message.author.name = "Research"
    message.webhook_id = 900
    message.application_id = None
    channel.fetch_message = AsyncMock(return_value=message)
    bot = _fake_bot(sessionmaker=MagicMock(), get_channel=MagicMock(return_value=channel))
    bot.http = MagicMock()
    bot.http.channel_webhooks = AsyncMock(
        return_value=[{"id": "900", "application_id": str(_BOT_USER_ID), "channel_id": "456"}]
    )
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=123,
        channel_id=456,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="👍",
        message_author_id=900,
    )
    assert await cog._possible_own_author(payload)  # pyright: ignore[reportPrivateUsage]
    assert await cog._is_bot_message_via_fetch(payload, bot_user_id=_BOT_USER_ID)  # pyright: ignore[reportPrivateUsage]
    bot.http.channel_webhooks.assert_awaited_once_with(456)


async def test_unresolved_webhook_application_is_not_memoized_false() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    message.author.id = 900
    message.webhook_id = 900
    message.author.name = "Research"
    message.application_id = None
    channel.fetch_message = AsyncMock(return_value=message)
    bot = _fake_bot(sessionmaker=MagicMock(), get_channel=MagicMock(return_value=channel))
    cog = FeedbackReactionCog(bot)
    cog._is_recorded_agent_post = AsyncMock(return_value=False)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    payload = _build_payload(
        message_id=123,
        channel_id=456,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="👍",
        message_author_id=None,
    )
    assert not await cog._is_bot_message_via_fetch(payload, bot_user_id=_BOT_USER_ID)  # pyright: ignore[reportPrivateUsage]
    assert 123 not in cog._author_is_bot  # pyright: ignore[reportPrivateUsage]


async def test_recorded_webhook_post_survives_unavailable_webhook_lookup() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    message.author.id = 900
    message.author.name = "Research"
    message.webhook_id = 900
    message.application_id = None
    channel.fetch_message = AsyncMock(return_value=message)
    bot = _fake_bot(sessionmaker=MagicMock(), get_channel=MagicMock(return_value=channel))
    cog = FeedbackReactionCog(bot)
    cog._is_recorded_agent_post = AsyncMock(return_value=True)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    payload = _build_payload(
        message_id=123,
        channel_id=456,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="👍",
        message_author_id=900,
    )
    assert await cog._is_bot_message_via_fetch(payload, bot_user_id=_BOT_USER_ID)  # pyright: ignore[reportPrivateUsage]


def _build_payload(
    *,
    message_id: int,
    channel_id: int,
    user_id: int,
    guild_id: int | None,
    emoji_name: str,
    emoji_id: int | None = None,
    message_author_id: int | None = None,
) -> discord.RawReactionActionEvent:
    """Construct a REAL RawReactionActionEvent from a bare gateway-shaped dict.

    Omitting `message_author_id` here is exactly how the gateway omits it,
    so tests exercising the fallback go through the real accessor rather
    than a stubbed attribute.
    """
    data: dict[str, object] = {
        "message_id": message_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "type": 0,
    }
    if guild_id is not None:
        data["guild_id"] = guild_id
    if message_author_id is not None:
        data["message_author_id"] = message_author_id
    emoji = discord.PartialEmoji(name=emoji_name, id=emoji_id)
    return discord.RawReactionActionEvent(
        data,  # type: ignore[arg-type]  # deliberately a bare gateway-shaped dict, not the full TypedDict -- see module docstring
        emoji,
        "REACTION_ADD",
    )


def _fake_bot(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    get_channel: Any = None,
    fetch_channel: Any = None,
    get_user: Any = None,
    fetch_user: Any = None,
) -> Any:
    """Minimal DaimonBot stand-in: only user/runtime/get_channel/fetch_channel/
    get_user/fetch_user are read."""
    return SimpleNamespace(
        user=SimpleNamespace(id=_BOT_USER_ID),
        application_id=_BOT_USER_ID,
        runtime=SimpleNamespace(sessionmaker=sessionmaker),
        get_channel=get_channel or MagicMock(return_value=None),
        fetch_channel=fetch_channel or AsyncMock(),
        get_user=get_user or MagicMock(return_value=None),
        fetch_user=fetch_user or AsyncMock(),
    )


def _spy_on_record_vote(monkeypatch: pytest.MonkeyPatch) -> list[RecordVoteResult]:
    """Wrap the real `record_vote` the Cog calls, capturing every result.

    An empty list after the handler runs means `record_vote` was never
    reached -- exactly the "writes nothing" assertion every negative test
    needs, with no DB read at all.
    """
    calls: list[RecordVoteResult] = []
    original = feedback_reactions.record_vote

    async def _spy(*args: Any, **kwargs: Any) -> RecordVoteResult:
        result = await original(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(feedback_reactions, "record_vote", _spy)
    return calls


# --- basic vote persistence -----------------------------------------------


async def test_thumbs_down_by_guild_member_writes_one_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1001,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    assert len(calls) == 1, "exactly one vote must be recorded"
    row = calls[0].row
    assert row.tenant_id == tenant.id, "recorded row must carry the resolved tenant"
    assert row.message_id == "1001", "recorded row must carry the reacted-to message id"
    assert row.channel_id == "2001", "recorded row must carry the channel id"
    assert row.platform_user_id == str(_REACTOR_ID), (
        "recorded row must carry the reactor's platform id"
    )
    assert row.vote == "down", "a thumbs-down reaction must record a down vote"


async def test_thumbs_up_writes_a_row_with_vote_up(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1002,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS UP SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    assert len(calls) == 1, "exactly one vote must be recorded"
    assert calls[0].row.vote == "up", "a thumbs-up reaction must record an up vote"


# --- the undocumented-field fallback ---------------------------------------


async def test_fallback_fetch_message_authored_by_bot_writes_a_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    fetched_message = MagicMock()
    fetched_message.author.id = _BOT_USER_ID
    fake_channel = MagicMock(spec=discord.abc.Messageable)
    fake_channel.fetch_message = AsyncMock(return_value=fetched_message)
    bot = _fake_bot(
        sessionmaker=db_session_factory, get_channel=MagicMock(return_value=fake_channel)
    )
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1003,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=None,
    )

    await cog.on_raw_reaction_add(payload)

    assert len(calls) == 1, (
        "the fallback must record a vote when fetch_message confirms bot authorship"
    )
    fake_channel.fetch_message.assert_awaited_once_with(1003)


async def test_fallback_fetch_message_authored_by_someone_else_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    fetched_message = MagicMock()
    fetched_message.author.id = 123456
    fake_channel = MagicMock(spec=discord.abc.Messageable)
    fake_channel.fetch_message = AsyncMock(return_value=fetched_message)
    bot = _fake_bot(
        sessionmaker=db_session_factory, get_channel=MagicMock(return_value=fake_channel)
    )
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1004,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=None,
    )

    await cog.on_raw_reaction_add(payload)

    assert calls == [], "a reaction to a human-authored message must never be recorded"


async def test_fallback_fetch_message_raises_not_found_writes_nothing_and_does_not_raise(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    fake_channel = MagicMock(spec=discord.abc.Messageable)
    fake_channel.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "message not found")
    )
    bot = _fake_bot(
        sessionmaker=db_session_factory, get_channel=MagicMock(return_value=fake_channel)
    )
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1005,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=None,
    )

    await cog.on_raw_reaction_add(payload)  # must not raise

    assert calls == [], "an unverifiable author must never resolve to 'assume ours'"


async def test_fallback_fetches_once_for_repeated_reactions_on_the_same_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memo is the bound on REST amplification if the gateway field goes away."""
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    _spy_on_record_vote(monkeypatch)
    fetched_message = MagicMock()
    fetched_message.author.id = 123456  # human-authored: the common case
    fake_channel = MagicMock(spec=discord.abc.Messageable)
    fake_channel.fetch_message = AsyncMock(return_value=fetched_message)
    bot = _fake_bot(
        sessionmaker=db_session_factory, get_channel=MagicMock(return_value=fake_channel)
    )
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1016,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=None,
    )

    await cog.on_raw_reaction_add(payload)
    await cog.on_raw_reaction_add(payload)
    await cog.on_raw_reaction_add(payload)

    assert fake_channel.fetch_message.await_count == 1, (
        "a resolved author verdict must be reused, not re-fetched on every reaction"
    )


async def test_fallback_refetches_after_a_failed_fetch_is_not_memoized(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient failure must not permanently suppress later votes on that message."""
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    fetched_message = MagicMock()
    fetched_message.author.id = _BOT_USER_ID
    fake_channel = MagicMock(spec=discord.abc.Messageable)
    fake_channel.fetch_message = AsyncMock(
        side_effect=[
            discord.NotFound(MagicMock(status=404), "message not found"),
            fetched_message,
        ]
    )
    bot = _fake_bot(
        sessionmaker=db_session_factory, get_channel=MagicMock(return_value=fake_channel)
    )
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1017,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=None,
    )

    await cog.on_raw_reaction_add(payload)
    await cog.on_raw_reaction_add(payload)

    assert fake_channel.fetch_message.await_count == 2, "a failed fetch must not be memoized"
    assert len(calls) == 1, "the retry that resolved a bot author must record the vote"


async def test_the_author_memo_is_bounded_and_evicts_the_oldest_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins that the memo cannot grow without bound across a long-running process."""
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    _spy_on_record_vote(monkeypatch)
    monkeypatch.setattr(feedback_reactions, "_AUTHOR_CACHE_MAX_ENTRIES", 2)
    fetched_message = MagicMock()
    fetched_message.author.id = 123456
    fake_channel = MagicMock(spec=discord.abc.Messageable)
    fake_channel.fetch_message = AsyncMock(return_value=fetched_message)
    bot = _fake_bot(
        sessionmaker=db_session_factory, get_channel=MagicMock(return_value=fake_channel)
    )
    cog = FeedbackReactionCog(bot)

    def _payload_for(message_id: int) -> discord.RawReactionActionEvent:
        return _build_payload(
            message_id=message_id,
            channel_id=2001,
            user_id=_REACTOR_ID,
            guild_id=_GUILD_ID,
            emoji_name="\N{THUMBS DOWN SIGN}",
            message_author_id=None,
        )

    for message_id in (1018, 1019, 1020):
        await cog.on_raw_reaction_add(_payload_for(message_id))
    await cog.on_raw_reaction_add(_payload_for(1018))

    assert fake_channel.fetch_message.await_count == 4, (
        "the oldest entry must be evicted at the cap, so the first message re-fetches"
    )


async def test_fast_path_does_not_call_fetch_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    fake_channel = MagicMock(spec=discord.abc.Messageable)
    fake_channel.fetch_message = AsyncMock()
    bot = _fake_bot(
        sessionmaker=db_session_factory, get_channel=MagicMock(return_value=fake_channel)
    )
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1006,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    fake_channel.fetch_message.assert_not_called()
    assert len(calls) == 1, "the fast path must still record the vote"


# --- pure gates --------------------------------------------------------------


async def test_bots_own_reaction_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1007,
        channel_id=2001,
        user_id=_BOT_USER_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    assert calls == [], "the bot's own seeded reactions must never count as votes"


async def test_reaction_with_no_guild_id_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1008,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=None,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)  # must not raise, no tenant to check against

    assert calls == [], "a reaction with no guild resolves no tenant and must not be recorded"


async def test_non_vote_emoji_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1009,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{SMILING FACE WITH SMILING EYES}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    assert calls == [], "a non-vote emoji must never be recorded"


async def test_custom_emoji_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1010,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="x",
        emoji_id=123,
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    assert calls == [], "a custom emoji must never be recorded, even if it happens to be a thumb"


async def test_guild_with_no_tenant_row_writes_nothing_and_does_not_raise(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unprovisioned_guild_id = 777777777777777777
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1011,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=unprovisioned_guild_id,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)  # must not raise

    assert calls == [], "a reaction in an unprovisioned guild must not be recorded"


# --- attribution: session hint + account resolution --------------------------


async def test_ma_session_id_populated_from_newest_live_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id=str(_REACTOR_ID)
    )
    await db_session.commit()
    async with db_session_factory() as seed_session:
        await create_thread_session(
            seed_session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="2001",
            account_id=principal.account_id,
            ma_session_id="sesn_live_001",
        )
        await seed_session.commit()

    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1012,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    assert len(calls) == 1, "vote must be recorded"
    assert calls[0].row.ma_session_id == "sesn_live_001", (
        "ma_session_id must be populated from the newest live session in that channel"
    )


async def test_ma_session_id_is_none_when_no_thread_session_exists(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1013,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    assert len(calls) == 1, "vote must be recorded"
    assert calls[0].row.ma_session_id is None, (
        "ma_session_id must be None when no thread session exists"
    )


async def test_account_id_is_none_for_unknown_reactor_and_no_principal_is_created(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1014,
        channel_id=2001,
        user_id=_OTHER_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    assert len(calls) == 1, "vote must be recorded even for an unknown reactor"
    assert calls[0].row.account_id is None, (
        "account_id must be None for a reactor with no platform principal"
    )
    async with db_session_factory() as verify_session:
        principal = await find_platform_principal(
            verify_session,
            tenant_id=tenant.id,
            platform="discord",
            external_id=str(_OTHER_REACTOR_ID),
        )
    assert principal is None, (
        "a bystander's reaction must never mint a permanent platform principal record"
    )


async def test_account_id_populated_for_known_reactor(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id=str(_REACTOR_ID)
    )
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    bot = _fake_bot(sessionmaker=db_session_factory)
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=1015,
        channel_id=2001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    assert len(calls) == 1, "vote must be recorded"
    assert calls[0].row.account_id == principal.account_id, (
        "account_id must be populated for a reactor with an existing platform principal"
    )


# --- private follow-up on a genuinely new down-vote --------------------------


async def test_first_thumbs_down_sends_exactly_one_private_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    _spy_on_record_vote(monkeypatch)
    recipient = MagicMock()
    recipient.send = AsyncMock()
    bot = _fake_bot(sessionmaker=db_session_factory, get_user=MagicMock(return_value=recipient))
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=2001,
        channel_id=3001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    recipient.send.assert_awaited_once()
    kwargs = recipient.send.call_args.kwargs
    content = kwargs["content"]
    assert str(_GUILD_ID) in content, "the link must carry the guild id"
    assert "3001" in content, "the link must carry the channel id"
    assert "2001" in content, "the link must carry the message id"
    view = kwargs["view"]
    assert len(view.children) == 1, "the view must carry exactly one item"
    button = view.children[0]
    assert CUSTOM_ID_PATTERN.fullmatch(button.custom_id) is not None, (
        "the button's custom_id must fullmatch the feedback grammar"
    )


async def test_repeat_thumbs_down_by_same_person_sends_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    recipient = MagicMock()
    recipient.send = AsyncMock()
    bot = _fake_bot(sessionmaker=db_session_factory, get_user=MagicMock(return_value=recipient))
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=2002,
        channel_id=3001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)
    await cog.on_raw_reaction_add(payload)

    assert recipient.send.await_count == 1, (
        "a repeat down-vote must not send a second private message"
    )
    assert len(calls) == 2, "record_vote is still called each time -- only the DM is suppressed"
    assert calls[0].row.id == calls[1].row.id, (
        "a repeat vote upserts the SAME row, never a second one"
    )


async def test_flip_from_up_to_down_sends_exactly_one_private_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    _spy_on_record_vote(monkeypatch)
    recipient = MagicMock()
    recipient.send = AsyncMock()
    bot = _fake_bot(sessionmaker=db_session_factory, get_user=MagicMock(return_value=recipient))
    cog = FeedbackReactionCog(bot)
    up_payload = _build_payload(
        message_id=2003,
        channel_id=3001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS UP SIGN}",
        message_author_id=_BOT_USER_ID,
    )
    down_payload = _build_payload(
        message_id=2003,
        channel_id=3001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(up_payload)
    await cog.on_raw_reaction_add(down_payload)

    assert recipient.send.await_count == 1, "a flip from up to down must count as a new down-vote"


async def test_flip_from_down_to_up_sends_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    _spy_on_record_vote(monkeypatch)
    recipient = MagicMock()
    recipient.send = AsyncMock()
    bot = _fake_bot(sessionmaker=db_session_factory, get_user=MagicMock(return_value=recipient))
    cog = FeedbackReactionCog(bot)
    down_payload = _build_payload(
        message_id=2004,
        channel_id=3001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )
    up_payload = _build_payload(
        message_id=2004,
        channel_id=3001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS UP SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(down_payload)
    await cog.on_raw_reaction_add(up_payload)

    assert recipient.send.await_count == 1, "only the down-vote should have triggered a prompt"


async def test_thumbs_up_alone_sends_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    _spy_on_record_vote(monkeypatch)
    recipient = MagicMock()
    recipient.send = AsyncMock()
    bot = _fake_bot(sessionmaker=db_session_factory, get_user=MagicMock(return_value=recipient))
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=2005,
        channel_id=3001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS UP SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)

    recipient.send.assert_not_awaited()


async def test_closed_dms_still_leave_the_vote_recorded(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    calls = _spy_on_record_vote(monkeypatch)
    recipient = MagicMock()
    recipient.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "Cannot send messages to this user")
    )
    bot = _fake_bot(sessionmaker=db_session_factory, get_user=MagicMock(return_value=recipient))
    cog = FeedbackReactionCog(bot)
    payload = _build_payload(
        message_id=2006,
        channel_id=3001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload)  # must not raise

    assert len(calls) == 1, (
        "a reactor with closed direct messages must still have their vote recorded"
    )


async def test_two_different_people_each_receive_one_private_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_tenant(db_session, workspace_id=str(_GUILD_ID))
    await db_session.commit()
    _spy_on_record_vote(monkeypatch)
    recipients: dict[int, MagicMock] = {
        _REACTOR_ID: MagicMock(send=AsyncMock()),
        _OTHER_REACTOR_ID: MagicMock(send=AsyncMock()),
    }

    def _lookup_recipient(user_id: int) -> MagicMock:
        return recipients[user_id]

    bot = _fake_bot(
        sessionmaker=db_session_factory,
        get_user=MagicMock(side_effect=_lookup_recipient),
    )
    cog = FeedbackReactionCog(bot)
    payload_a = _build_payload(
        message_id=2007,
        channel_id=3001,
        user_id=_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )
    payload_b = _build_payload(
        message_id=2007,
        channel_id=3001,
        user_id=_OTHER_REACTOR_ID,
        guild_id=_GUILD_ID,
        emoji_name="\N{THUMBS DOWN SIGN}",
        message_author_id=_BOT_USER_ID,
    )

    await cog.on_raw_reaction_add(payload_a)
    await cog.on_raw_reaction_add(payload_b)

    recipients[_REACTOR_ID].send.assert_awaited_once()
    recipients[_OTHER_REACTOR_ID].send.assert_awaited_once()
