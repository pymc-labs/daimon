"""Feedback counted by agent: votes and reasons, and nothing about who or what was said."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from daimon.core.authz import Subject
from daimon.core.feedback_summary import FeedbackNotAllowed, feedback_summary
from daimon.core.message_feedback import Vote
from daimon.core.stores import message_feedback as store
from daimon.testing.factories import make_tenant, make_thread_session
from sqlalchemy.ext.asyncio import AsyncSession

ADMIN = Subject(is_admin=True)
START = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
END = dt.datetime(2100, 1, 1, tzinfo=dt.UTC)


async def _vote(
    db: AsyncSession, tenant_id: uuid.UUID, n: int, session_id: str, vote: Vote, reasons: list[str]
) -> None:
    result = await store.record_vote(
        db,
        tenant_id=tenant_id,
        platform="discord",
        message_id=f"m{n}",
        channel_id="c",
        platform_user_id=f"u{n}",
        account_id=None,
        ma_session_id=session_id,
        vote=vote,
    )
    if reasons:
        await store.attach_feedback_details(
            db,
            feedback_id=result.row.id,
            platform_user_id=f"u{n}",
            feedback_text="private words",
            feedback_reasons=reasons,
        )


async def test_votes_and_reasons_count_per_agent_and_carry_no_person_or_text(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await make_thread_session(db_session, tenant=tenant, ma_session_id="s1", ma_agent_id="ag_a")
    await make_thread_session(db_session, tenant=tenant, ma_session_id="s2", ma_agent_id="ag_b")
    await _vote(db_session, tenant.id, 1, "s1", "up", [])
    await _vote(db_session, tenant.id, 2, "s1", "down", ["inaccurate", "too_slow"])
    await _vote(db_session, tenant.id, 3, "s1", "down", ["inaccurate"])
    await _vote(db_session, tenant.id, 4, "s2", "down", ["made_up_code"])
    await _vote(db_session, tenant.id, 5, "gone", "up", [])

    found = await feedback_summary(
        db_session, subject=ADMIN, tenant_id=tenant.id, since=START, until=END
    )

    by_agent = {f.ma_agent_id: f for f in found}
    a = by_agent["ag_a"]
    assert (a.up, a.down) == (1, 2), "votes count per agent"
    assert a.reasons == {"inaccurate": 2, "too_slow": 1}, "each picked reason counts once"
    assert by_agent["ag_b"].reasons == {}, "an unknown reason code is not counted"
    assert by_agent[None].up == 1, "a vote whose session is unknown counts under no agent"
    assert "private words" not in repr(found), "no text leaves the summary"


async def test_only_a_workspace_admin_reads_feedback_and_other_workspaces_never_count(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    await _vote(db_session, other.id, 1, "s", "up", [])
    for who in (
        Subject(),
        Subject(is_admin=True, via_agent_key=True),
        Subject(is_admin=True, client=True),
    ):
        with pytest.raises(FeedbackNotAllowed):
            await feedback_summary(
                db_session, subject=who, tenant_id=tenant.id, since=START, until=END
            )
    assert (
        await feedback_summary(
            db_session, subject=ADMIN, tenant_id=tenant.id, since=START, until=END
        )
        == []
    ), "another workspace's votes are not counted"
