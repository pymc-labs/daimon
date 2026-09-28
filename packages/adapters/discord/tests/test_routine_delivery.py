"""Discord's routine result poster (FEAT-085)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.routine_delivery import make_discord_routine_poster
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine, record_result
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = 424242


def _text_channel(*, guild_id: int = _GUILD, category_id: int | None = None) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=guild_id)
    channel.category_id = category_id
    channel.send = AsyncMock()
    return channel


async def _routine(db_session: AsyncSession, *, destination_id: str = "555") -> RoutineRow:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD))
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="1",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="go",
        destination_kind="channel",
        destination_id=destination_id,
    )
    await record_result(
        db_session, row.id, tail="All green @everyone.", error=None, delivery="pending"
    )
    await db_session.commit()
    return row.model_copy(
        update={
            "last_result_tail": "All green @everyone.",
            "delivery_payload": "All green @everyone.",
        }
    )


class _Dms:
    """Records DMs opened for the creator; `fail` makes opening raise."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, int, str]] = []
        self.fail = fail

    async def open(self, guild_id: int, user_id: int) -> Any:
        if self.fail:
            raise LookupError("not a member")
        dms = self

        class _Dm:
            async def send(self, *, content: str, allowed_mentions: object) -> None:
                dms.sent.append((guild_id, user_id, content))

        return _Dm()


def _poster(
    sm: async_sessionmaker[AsyncSession],
    channels: object | dict[int, object],
    *,
    dms: _Dms | None = None,
    dm_mode: str = "members",
) -> Any:
    from daimon.core.config import DirectMessagePolicy

    async def fetch(channel_id: int) -> object:
        found = channels.get(channel_id) if isinstance(channels, dict) else channels
        if found is None or isinstance(found, Exception):
            raise found or discord.NotFound(
                MagicMock(status=404, reason="Not Found"), "Unknown Channel"
            )
        return found

    return make_discord_routine_poster(
        sm,
        fetch_channel=fetch,
        open_dm=(dms or _Dms()).open,
        dm_policy=lambda row: DirectMessagePolicy(mode=dm_mode),  # type: ignore[arg-type]
    )


def _uncached_thread(*, parent_id: int = 444) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.guild = SimpleNamespace(id=_GUILD)
    thread.parent = None  # not in the bot's cache
    thread.parent_id = parent_id
    thread.send = AsyncMock()
    return thread


async def test_posts_the_tail_with_mentions_disabled(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    channel = _text_channel()

    outcome = await _poster(db_session_factory, channel)(row)

    assert outcome.status == "delivered"
    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"].endswith("All green @everyone.")
    mentions = kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles


async def test_a_channel_in_a_protected_category_falls_back_to_a_dm(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(protected_category_ids=("77",)),
    )
    await db_session.commit()
    channel = _text_channel(category_id=77)
    dms = _Dms()

    outcome = await _poster(db_session_factory, channel, dms=dms)(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:protected_channel")
    channel.send.assert_not_awaited()
    (guild_id, user_id, content) = dms.sent[0]
    assert (guild_id, user_id) == (_GUILD, 1)
    assert "protected channel" in content and content.endswith("All green @everyone.")


async def test_an_uncached_thread_parent_is_resolved_before_category_protection(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Review regression: a thread whose parent was not cached used to skip the
    category check and post into a protected category."""
    row = await _routine(db_session)
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(protected_category_ids=("77",)),
    )
    await db_session.commit()
    thread = _uncached_thread(parent_id=444)
    parent = _text_channel(category_id=77)

    outcome = await _poster(db_session_factory, {555: thread, 444: parent})(row)

    assert outcome.note == "dm_fallback:protected_channel"
    thread.send.assert_not_awaited()


async def test_a_thread_whose_parent_cannot_be_resolved_is_not_posted_to(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    thread = _uncached_thread(parent_id=444)

    outcome = await _poster(db_session_factory, {555: thread})(row)  # parent lookup 404s

    assert outcome.note == "dm_fallback:destination_unavailable"
    thread.send.assert_not_awaited()


async def test_no_dm_when_the_dm_policy_disallows_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    dms = _Dms()
    gone = discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")

    outcome = await _poster(db_session_factory, gone, dms=dms, dm_mode="disabled")(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    assert dms.sent == []


async def test_an_archived_tenant_posts_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from datetime import UTC, datetime

    from daimon.core.defaults.provisioning import archive_tenant

    row = await _routine(db_session)
    await archive_tenant(db_session_factory, tenant_id=row.tenant_id, now=datetime.now(UTC))
    channel = _text_channel()

    outcome = await _poster(db_session_factory, channel)(row)

    assert (outcome.status, outcome.note) == ("skipped", "tenant_archived")
    channel.send.assert_not_awaited()


async def test_a_creator_no_longer_allowed_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(invoker_user_ids=("SOMEONE_ELSE",)),
    )
    await db_session.commit()
    channel = _text_channel()

    outcome = await _poster(db_session_factory, channel)(row)

    assert (outcome.status, outcome.note) == ("skipped", "invoker_not_allowed")
    channel.send.assert_not_awaited()


async def test_a_channel_in_another_guild_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    channel = _text_channel(guild_id=999)

    outcome = await _poster(db_session_factory, channel)(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:destination_unavailable")
    channel.send.assert_not_awaited()


async def test_a_missing_channel_is_skipped(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    gone = discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")

    outcome = await _poster(db_session_factory, gone, dms=_Dms(fail=True))(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
