"""Tidy limits and audit rows (`daimon.core.channel_tidy`) and the post ledger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.channel_tidy import (
    PER_HOUR_LIMIT,
    PER_TURN_LIMIT,
    TidyActor,
    TidyLimitReached,
    TidyTarget,
    record_tidy_actions,
    record_tidy_outcome,
)
from daimon.core.stores.agent_posts import get_post, list_posts_in, mark_deleted, record_post
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


def _actor(tenant_id: uuid.UUID, agent_id: uuid.UUID, turn: str) -> TidyActor:
    return TidyActor(
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        platform="discord",
        platform_user_id="42",
        turn_ref=turn,
    )


def _targets(n: int) -> list[TidyTarget]:
    return [TidyTarget(channel_id="222", message_id=str(i)) for i in range(n)]


async def test_the_turn_limit_counts_one_turn_and_the_hour_limit_counts_all(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    now = datetime.now(UTC)

    await record_tidy_actions(
        db_session,
        actor=_actor(tenant.id, agent, "origin:a"),
        tool_name="delete_message",
        operation="message.delete",
        targets=_targets(PER_TURN_LIMIT),
        now=now,
    )
    with pytest.raises(TidyLimitReached) as turn_err:
        await record_tidy_actions(
            db_session,
            actor=_actor(tenant.id, agent, "origin:a"),
            tool_name="delete_message",
            operation="message.delete",
            targets=_targets(1),
            now=now,
        )
    assert turn_err.value.scope == "turn", "the eleventh action in one turn hits the turn limit"

    for turn in range(1, PER_HOUR_LIMIT // PER_TURN_LIMIT):
        await record_tidy_actions(
            db_session,
            actor=_actor(tenant.id, agent, f"origin:{turn}"),
            tool_name="edit_message",
            operation="message.edit",
            targets=_targets(PER_TURN_LIMIT),
            now=now,
        )
    with pytest.raises(TidyLimitReached) as hour_err:
        await record_tidy_actions(
            db_session,
            actor=_actor(tenant.id, agent, "origin:fresh"),
            tool_name="edit_message",
            operation="message.edit",
            targets=_targets(1),
            now=now,
        )
    assert hour_err.value.scope == "hour", "a fresh turn still hits the hourly limit"

    await record_tidy_actions(
        db_session,
        actor=_actor(tenant.id, agent, "origin:later"),
        tool_name="edit_message",
        operation="message.edit",
        targets=_targets(1),
        now=now + timedelta(hours=1, seconds=1),
    )
    await record_tidy_actions(
        db_session,
        actor=_actor(tenant.id, uuid.uuid4(), "origin:a"),
        tool_name="edit_message",
        operation="message.edit",
        targets=_targets(1),
        now=now,
    )
    rows = await list_events(db_session, tenant_id=tenant.id, limit=1000)
    assert len(rows) == PER_HOUR_LIMIT + 2, "an hour later, or another agent, starts afresh"


async def test_refusals_are_audited_but_do_not_spend_the_budget(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    actor = _actor(tenant.id, agent, "origin:a")
    now = datetime.now(UTC)
    for i in range(PER_TURN_LIMIT + 5):
        await record_tidy_outcome(
            db_session,
            actor=actor,
            tool_name="delete_message",
            operation="message.delete",
            outcome="denied",
            reason="not_posted_by_agent",
            target=TidyTarget(channel_id="222", message_id=str(i)),
            now=now,
        )
    await record_tidy_actions(
        db_session,
        actor=actor,
        tool_name="delete_message",
        operation="message.delete",
        targets=_targets(PER_TURN_LIMIT),
        now=now,
    )
    rows = await list_events(db_session, tenant_id=tenant.id, limit=1000)
    assert {r.target_channel_id for r in rows} == {"222"}, "every row carries its target"
    assert {r.turn_ref for r in rows} == {"origin:a"}, "every row carries its turn"


async def test_the_ledger_keeps_the_first_owner_and_hides_deleted_posts(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    first, second = uuid.uuid4(), uuid.uuid4()
    for agent in (first, second):
        await record_post(
            db_session,
            tenant_id=tenant.id,
            platform="slack",
            channel_id="C1",
            message_id="1.1",
            agent_id=agent,
        )
    post = await get_post(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C1", message_id="1.1"
    )
    assert post is not None and post.agent_id == first, "a second record never takes ownership"

    await mark_deleted(db_session, post_ids=[post.id], now=datetime.now(UTC))
    assert (
        await list_posts_in(
            db_session, tenant_id=tenant.id, platform="slack", channel_id="C1", message_ids=["1.1"]
        )
        == []
    ), "a deleted post is not listed"
