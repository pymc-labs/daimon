"""Real-Postgres tests for the once-ever connect-prompt marker store."""

from __future__ import annotations

from datetime import UTC, datetime

from daimon.core.stores.slack_connect_prompts import mark_connect_prompted, was_connect_prompted
from sqlalchemy.ext.asyncio import AsyncSession


async def test_was_connect_prompted_false_before_mark(db_session: AsyncSession) -> None:
    assert await was_connect_prompted(db_session, team_id="T1", slack_user_id="U1") is False, (
        "unprompted user must read False"
    )


async def test_mark_then_was_prompted_true(db_session: AsyncSession) -> None:
    await mark_connect_prompted(
        db_session, team_id="T1", slack_user_id="U1", now=datetime.now(tz=UTC)
    )
    assert await was_connect_prompted(db_session, team_id="T1", slack_user_id="U1") is True, (
        "marked user must read True"
    )


async def test_mark_twice_does_not_raise(db_session: AsyncSession) -> None:
    now = datetime.now(tz=UTC)
    await mark_connect_prompted(db_session, team_id="T1", slack_user_id="U1", now=now)
    await mark_connect_prompted(db_session, team_id="T1", slack_user_id="U1", now=now)
    assert await was_connect_prompted(db_session, team_id="T1", slack_user_id="U1") is True, (
        "double-mark (concurrent nudge race) must be ON CONFLICT DO NOTHING"
    )
