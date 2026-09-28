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
        created_by_user_id="U1",
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
    return row.model_copy(update={"last_result_tail": "All green @everyone."})


def _poster(sm: async_sessionmaker[AsyncSession], channel: object) -> Any:
    async def fetch(channel_id: int) -> object:
        if isinstance(channel, Exception):
            raise channel
        return channel

    return make_discord_routine_poster(sm, fetch_channel=fetch)


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


async def test_a_channel_in_a_protected_category_is_refused(
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

    outcome = await _poster(db_session_factory, channel)(row)

    assert (outcome.status, outcome.note) == ("skipped", "protected_channel")
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

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    channel.send.assert_not_awaited()


async def test_a_missing_channel_is_skipped(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    gone = discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")

    outcome = await _poster(db_session_factory, gone)(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
