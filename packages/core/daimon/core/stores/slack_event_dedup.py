"""Exactly-once admission gate for inbound Slack events (STURN-02).

Dedup key is the logical event triple (team_id, channel, event_ts), NOT
envelope_id. Slack Socket Mode reconnect redelivers the same logical event
with a fresh envelope_id, so keying on envelope_id would admit duplicates.

No try/except — DB exceptions propagate to the adapter listener boundary per
the project's error-propagation rule.
"""

from __future__ import annotations

from typing import Any, cast

from daimon.core._models import SlackEventDedup
from sqlalchemy import CursorResult
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
