"""Tests for daimon.core.slack_event_dedup_sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core._models import SlackEventDedup
from daimon.core.slack_event_dedup_sweep import sweep_expired_slack_event_dedup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)


async def test_sweep_deletes_row_older_than_retention_window(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    db_session.add(
        SlackEventDedup(
            team_id="T1",
            channel="C1",
            event_ts="1.1",
            created_at=NOW - timedelta(days=8),
        )
    )
    await db_session.commit()

    count = await sweep_expired_slack_event_dedup(db_session_factory, now=NOW)

    assert count == 1, "a row older than the 7-day retention window must be deleted"


async def test_sweep_leaves_row_inside_retention_window_untouched(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    db_session.add(
        SlackEventDedup(
            team_id="T1",
            channel="C1",
            event_ts="1.1",
            created_at=NOW - timedelta(days=1),
        )
    )
    await db_session.commit()

    count = await sweep_expired_slack_event_dedup(db_session_factory, now=NOW)

    assert count == 0, "a row inside the retention window must survive"


async def test_sweep_near_boundary_row_survives(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    db_session.add(
        SlackEventDedup(
            team_id="T1",
            channel="C1",
            event_ts="1.1",
            created_at=NOW - timedelta(days=6, hours=23),
        )
    )
    await db_session.commit()

    count = await sweep_expired_slack_event_dedup(db_session_factory, now=NOW)

    assert count == 0, (
        "a row one hour inside the 7-day window must survive — the window must "
        "stay comfortably wider than Slack's own worst-case 24-hour redelivery bound"
    )


async def test_sweep_outcome_changes_with_the_injected_now(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    db_session.add(
        SlackEventDedup(
            team_id="T1",
            channel="C1",
            event_ts="1.1",
            created_at=NOW - timedelta(days=8),
        )
    )
    await db_session.commit()

    before_retention_elapsed = await sweep_expired_slack_event_dedup(
        db_session_factory, now=NOW - timedelta(days=8)
    )
    after_retention_elapsed = await sweep_expired_slack_event_dedup(db_session_factory, now=NOW)

    assert before_retention_elapsed == 0, (
        "at the row's own creation instant, no clock has yet been read internally — "
        "the row is not aged out"
    )
    assert after_retention_elapsed == 1, (
        "the same seeded row is deleted once `now` moves past the retention window — "
        "proving the outcome is decided by the injected `now`, not an internal clock read"
    )
