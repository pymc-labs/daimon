"""Real-Postgres round-trip tests for the slack_event_dedup store.

Covers both the admission gate and the TTL prune:

`insert_if_new` (three tests):
1. First insert on a fresh (team_id, channel, event_ts) triple → True.
2. Second insert on the same triple → False (ON CONFLICT DO NOTHING, rowcount 0).
3. Different event_ts for the same (team_id, channel) → True (distinct logical event).

Dedup is on the logical key, NOT envelope_id: Slack Socket Mode reconnect redelivers
the same logical event with a fresh envelope_id, so only the content triple matters.

`delete_event_dedup_older_than` (four tests): rows strictly older than the cutoff
are deleted; a row at or after the cutoff survives; `limit` caps one call's
deletions; an empty table returns 0 without raising.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core._models import SlackEventDedup
from daimon.core.stores.slack_event_dedup import (
    delete_event_dedup_older_than,
    insert_if_new,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

CUTOFF = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)


async def test_insert_if_new_first_insert_when_triple_is_new_returns_true(
    db_session: AsyncSession,
) -> None:
    result = await insert_if_new(db_session, team_id="T1", channel="C1", event_ts="100.1")
    assert result is True, "first insert on a fresh triple must return True"


async def test_insert_if_new_second_insert_when_triple_already_exists_returns_false(
    db_session: AsyncSession,
) -> None:
    await insert_if_new(db_session, team_id="T1", channel="C1", event_ts="100.1")
    result = await insert_if_new(db_session, team_id="T1", channel="C1", event_ts="100.1")
    assert result is False, (
        "second insert on the same (team_id, channel, event_ts) must return False "
        "(ON CONFLICT DO NOTHING, rowcount 0)"
    )


async def test_insert_if_new_different_event_ts_when_same_team_and_channel_returns_true(
    db_session: AsyncSession,
) -> None:
    await insert_if_new(db_session, team_id="T1", channel="C1", event_ts="100.1")
    result = await insert_if_new(db_session, team_id="T1", channel="C1", event_ts="100.2")
    assert result is True, (
        "a different event_ts for the same (team_id, channel) must return True — "
        "it is a distinct logical event"
    )


async def test_delete_event_dedup_older_than_deletes_rows_strictly_before_cutoff(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        SlackEventDedup(
            team_id="T1",
            channel="C1",
            event_ts="1.1",
            created_at=CUTOFF - timedelta(days=1),
        )
    )
    await db_session.flush()

    count = await delete_event_dedup_older_than(db_session, cutoff=CUTOFF)

    assert count == 1, "a row strictly older than the cutoff must be deleted"
    remaining = (await db_session.execute(select(SlackEventDedup))).scalars().all()
    assert remaining == [], "the deleted row must no longer be present"


async def test_delete_event_dedup_older_than_leaves_row_at_or_after_cutoff(
    db_session: AsyncSession,
) -> None:
    db_session.add(SlackEventDedup(team_id="T1", channel="C1", event_ts="1.1", created_at=CUTOFF))
    await db_session.flush()

    count = await delete_event_dedup_older_than(db_session, cutoff=CUTOFF)

    assert count == 0, (
        "the prune must never delete a row Slack could still redeliver — a row at "
        "or after the cutoff must survive"
    )
    remaining = (await db_session.execute(select(SlackEventDedup))).scalars().all()
    assert len(remaining) == 1, "the surviving row must still be present"


async def test_delete_event_dedup_older_than_limit_caps_one_calls_deletions(
    db_session: AsyncSession,
) -> None:
    for i in range(3):
        db_session.add(
            SlackEventDedup(
                team_id="T1",
                channel="C1",
                event_ts=f"{i}.1",
                created_at=CUTOFF - timedelta(days=1),
            )
        )
    await db_session.flush()

    count = await delete_event_dedup_older_than(db_session, cutoff=CUTOFF, limit=2)

    assert count == 2, "limit must cap the rowcount returned by one call"
    remaining = (await db_session.execute(select(SlackEventDedup))).scalars().all()
    assert len(remaining) == 1, "rows beyond the limit must survive this call"


async def test_delete_event_dedup_older_than_empty_table_returns_zero(
    db_session: AsyncSession,
) -> None:
    count = await delete_event_dedup_older_than(db_session, cutoff=CUTOFF)

    assert count == 0, "an empty table must return 0 without raising"
