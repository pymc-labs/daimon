"""Double clicks on the Slack feedback controls — real Postgres, separate connections.

The shared ``db_session_factory`` binds every session to one connection, so a
real race needs its own engine: each click's handler runs on its own
connection, as two Slack deliveries would in production.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from daimon.adapters.slack.feedback import (
    FEEDBACK_VOTE_DOWN,
    evaluate_feedback_text_submission,
    handle_feedback_vote,
    run_feedback_text_submission,
)
from daimon.testing.db import build_test_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from .harness import build_slack_runtime
from .test_feedback import (
    _TEAM_ID,
    _USER_ID,
    _rows,
    _seed_team,
    _submission_payload,
    _vote_payload,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def race_engine(db_engine: AsyncEngine, db_schema: str) -> AsyncIterator[AsyncEngine]:
    engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False), db_schema, poolclass=NullPool
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_a_concurrent_double_click_records_one_row(
    db_session: AsyncSession, race_engine: AsyncEngine, fake_slack_web_client: Any
) -> None:
    _tenant_id, key = await _seed_team(db_session)
    await db_session.commit()
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    runtime = build_slack_runtime(key, factory)

    await asyncio.gather(
        *(handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN)) for _ in range(4))
    )

    rows = await _rows(factory)
    assert len(rows) == 1 and rows[0]["vote"] == "down"


async def test_concurrent_submissions_for_one_answer_record_one_row(
    db_session: AsyncSession, race_engine: AsyncEngine, fake_slack_web_client: Any
) -> None:
    _tenant_id, key = await _seed_team(db_session)
    await db_session.commit()
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    runtime = build_slack_runtime(key, factory)

    await asyncio.gather(
        *(
            run_feedback_text_submission(
                runtime,
                team_id=_TEAM_ID,
                user_id=_USER_ID,
                decision=evaluate_feedback_text_submission(
                    _submission_payload(f"take {i}", message_ts="1700000001.000100")
                ),
            )
            for i in range(4)
        )
    )

    rows = await _rows(factory)
    assert len(rows) == 1
    assert rows[0]["feedback_text"] in {f"take {i}" for i in range(4)}
