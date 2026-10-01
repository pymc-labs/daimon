"""Exactly-once admission gate for inbound Slack events (STURN-02).

Dedup key is the logical event triple (team_id, channel, event_ts), NOT
envelope_id. Slack Socket Mode reconnect redelivers the same logical event
with a fresh envelope_id, so keying on envelope_id would admit duplicates.

No try/except — DB exceptions propagate to the adapter listener boundary per
the project's error-propagation rule.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from daimon.core._models import SlackEventDedup
from sqlalchemy import CursorResult, delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_if_new(
    session: AsyncSession,
    *,
    team_id: str,
    channel: str,
    event_ts: str,
) -> bool:
    """Insert the (team_id, channel, event_ts) triple if it does not exist.

    Returns True on a genuine first insert, False when the triple is already
    present (ON CONFLICT DO NOTHING, rowcount 0). The caller is responsible
    for committing the session after checking the return value.
    """
    stmt = (
        pg_insert(SlackEventDedup)
        .values(team_id=team_id, channel=channel, event_ts=event_ts)
        .on_conflict_do_nothing(index_elements=["team_id", "channel", "event_ts"])
    )
    result = await session.execute(stmt)
    await session.flush()
    return cast(CursorResult[Any], result).rowcount == 1


async def delete_event_dedup_for_team(session: AsyncSession, *, team_id: str) -> int:
    """Delete every slack_event_dedup row for a workspace. Idempotent.

    Returns rowcount; never raises on 0. Used by the Slack uninstall teardown.
    """
    result = await session.execute(
        delete(SlackEventDedup).where(SlackEventDedup.team_id == team_id)
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def delete_event_dedup_older_than(
    session: AsyncSession,
    *,
    cutoff: datetime,
    limit: int = 500,
) -> int:
    """Delete up to `limit` slack_event_dedup rows created before `cutoff`.

    Selects the target keys first with a `LIMIT`, then deletes by that key
    set, so one sweep tick cannot lock the whole table (mirrors
    `abandon_expired_wizard_sessions`). The primary key is the composite
    (team_id, channel, event_ts), not a single id column, so the delete uses
    a tuple `IN` over those three columns rather than an id list. Returns the
    rowcount.
    """
    key_stmt = (
        select(
            SlackEventDedup.team_id,
            SlackEventDedup.channel,
            SlackEventDedup.event_ts,
        )
        .where(SlackEventDedup.created_at < cutoff)
        .limit(limit)
    )
    keys = (await session.execute(key_stmt)).all()
    if not keys:
        return 0
    stmt = delete(SlackEventDedup).where(
        tuple_(SlackEventDedup.team_id, SlackEventDedup.channel, SlackEventDedup.event_ts).in_(keys)
    )
    result = await session.execute(stmt)
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount
