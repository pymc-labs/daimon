"""Real-Postgres round-trip tests for the slack_event_dedup store.

Tests the three admission behaviors of insert_if_new:
1. First insert on a fresh (team_id, channel, event_ts) triple → True.
2. Second insert on the same triple → False (ON CONFLICT DO NOTHING, rowcount 0).
3. Different event_ts for the same (team_id, channel) → True (distinct logical event).

Dedup is on the logical key, NOT envelope_id: Slack Socket Mode reconnect redelivers
the same logical event with a fresh envelope_id, so only the content triple matters.
"""

from __future__ import annotations

from daimon.core.stores.slack_event_dedup import insert_if_new
from sqlalchemy.ext.asyncio import AsyncSession


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
