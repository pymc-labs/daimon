"""Tidy limits and audit rows (`daimon.core.channel_tidy`) and the post ledger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core._models import AgentPostedMessage
from daimon.core.channel_tidy import (
    PER_HOUR_LIMIT,
    PER_TURN_LIMIT,
    TidyActor,
    TidyLimitReached,
    TidyTarget,
    record_tidy_actions,
    record_tidy_outcome,
)
from daimon.core.stores.agent_posts import (
    get_post,
    list_posts_in,
    list_recent_posts,
    list_reply_posts,
    mark_deleted,
    record_post,
    set_post_role,
)
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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


async def test_recent_posts_are_one_agents_live_messages_in_one_chat_newest_first(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    mine, other = uuid.uuid4(), uuid.uuid4()
    for channel, message, agent in [
        ("a:1", "1", mine),
        ("a:1", "2", mine),
        ("a:1", "3", mine),
        ("a:1", "4", other),
        ("a:2", "5", mine),
    ]:
        await record_post(
            db_session,
            tenant_id=tenant.id,
            platform="teams",
            channel_id=channel,
            message_id=message,
            agent_id=agent,
        )
    start = datetime.now(UTC)
    for i in range(1, 4):
        await db_session.execute(
            update(AgentPostedMessage)
            .where(AgentPostedMessage.message_id == str(i))
            .values(posted_at=start + timedelta(seconds=i))
        )
    [deleted] = await list_posts_in(
        db_session, tenant_id=tenant.id, platform="teams", channel_id="a:1", message_ids=["3"]
    )
    await mark_deleted(db_session, post_ids=[deleted.id], now=start)

    rows = await list_recent_posts(
        db_session, tenant_id=tenant.id, platform="teams", channel_id="a:1", agent_id=mine, limit=5
    )
    assert [r.message_id for r in rows] == ["2", "1"], (
        "newest first; deleted, other agents' and other chats' posts are left out"
    )


async def test_a_posts_role_is_kept_and_follows_an_edit(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    await record_post(
        db_session,
        tenant_id=tenant.id,
        platform="teams",
        channel_id="a:1",
        message_id="1",
        agent_id=agent,
        role="status",
    )
    await set_post_role(
        db_session,
        tenant_id=tenant.id,
        platform="teams",
        channel_id="a:1",
        message_id="1",
        role="answer",
    )
    [row] = await list_recent_posts(
        db_session, tenant_id=tenant.id, platform="teams", channel_id="a:1", agent_id=agent, limit=5
    )
    assert row.role == "answer", "a card edited into the answer is listed as the answer"


async def test_account_erasure_clears_tidy_hmacs_and_prune_removes_old_posts(
    db_session: AsyncSession,
) -> None:
    from daimon.core.stores.agent_posts import prune_posts
    from daimon.core.stores.security_audit import erase_account
    from daimon.testing.factories import make_account

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    actor = TidyActor(
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=account.id,
        platform="discord",
        platform_user_id="42",
        turn_ref="origin:a",
    )
    await record_tidy_actions(
        db_session,
        actor=actor,
        tool_name="delete_message",
        operation="message.delete",
        targets=[TidyTarget(channel_id="222", message_id="1", content_hmac="ab" * 32)],
        now=datetime.now(UTC),
    )
    await erase_account(db_session, tenant_id=tenant.id, account_id=account.id)
    (row,) = await list_events(db_session, tenant_id=tenant.id)
    assert (row.account_id, row.platform_user_id, row.content_hmac) == (None, None, None), (
        "erasing the account clears the HMAC with its identifiers"
    )

    await record_post(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="222",
        message_id="1",
        agent_id=actor.agent_id,
        content_hmac="cd" * 32,
    )
    removed = await prune_posts(
        db_session, tenant_id=tenant.id, older_than=datetime.now(UTC) + timedelta(seconds=1)
    )
    assert removed == 1, "post records expire with the audit retention"


async def test_a_turn_post_names_its_agent_requester_and_turn_and_settles_with_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.channel_tidy import record_turn_post
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.turn_card_intents import (
        create_turn_card_intent,
        record_turn_card_message,
        retire_turn_card_intent,
        turn_card_intent_is_active,
    )

    async with db_session_factory.begin() as s:
        tenant = await make_tenant(s)
        intent = await create_turn_card_intent(
            s, tenant_id=tenant.id, platform="discord", thread_id="444", turn_token=uuid.uuid4()
        )
        await record_turn_card_message(s, id=intent.id, message_id="1000")
    await record_turn_post(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        ma_agent_id="ag_x",
        channel_id="444",
        message_id="1000",
        requester_platform_user_id="42",
        source="turn",
        turn_card_intent_id=intent.id,
        parent_channel_id="222",
    )
    await record_turn_post(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        ma_agent_id="ag_x",
        channel_id="222",
        message_id="444",
        requester_platform_user_id="42",
        source="auto_thread",
    )
    async with db_session_factory() as s:
        card = await get_post(
            s, tenant_id=tenant.id, platform="discord", channel_id="444", message_id="1000"
        )
        thread = await get_post(
            s, tenant_id=tenant.id, platform="discord", channel_id="222", message_id="444"
        )
        assert card is not None and thread is not None, "both are recorded"
        agent = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_x")
        assert (card.agent_id, card.source, card.kind, card.turn_card_intent_id) == (
            agent,
            "turn",
            "message",
            intent.id,
        ), "a card names the turn's agent and the turn"
        assert (thread.source, thread.kind, thread.requester_platform_user_id) == (
            "auto_thread",
            "thread",
            "42",
        ), "an auto-opened thread names who opened it"
        assert await turn_card_intent_is_active(s, id=intent.id), "the turn is still running"
    async with db_session_factory.begin() as s:
        await retire_turn_card_intent(s, id=intent.id, expected_message_id="1000")
    async with db_session_factory() as s:
        assert not await turn_card_intent_is_active(s, id=intent.id), "a retired turn is over"
        assert not await turn_card_intent_is_active(s, id=uuid.uuid4()), "a pruned turn is over"


async def test_a_turn_post_that_cannot_be_recorded_does_not_fail_the_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.channel_tidy import record_turn_post

    # No such tenant: the insert fails its foreign key and is logged, not raised.
    await record_turn_post(
        db_session_factory,
        tenant_id=uuid.uuid4(),
        platform="discord",
        ma_agent_id="ag_x",
        channel_id="444",
        message_id="1000",
        requester_platform_user_id="42",
        source="turn",
        turn_card_intent_id=uuid.uuid4(),
    )


async def test_slack_turn_post_records_channel_and_thread_root(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.channel_tidy import record_turn_post

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
    intent_id = uuid.uuid4()
    await record_turn_post(
        db_session_factory,
        tenant_id=tenant.id,
        platform="slack",
        ma_agent_id="ag_ada",
        channel_id="C123",
        message_id="1700000001.000001",
        thread_ts="1700000000.000000",
        requester_platform_user_id="U123",
        source="turn",
        turn_card_intent_id=intent_id,
    )
    async with db_session_factory() as session:
        post = await get_post(
            session,
            tenant_id=tenant.id,
            platform="slack",
            channel_id="C123",
            message_id="1700000001.000001",
        )
    assert post is not None
    assert post.thread_ts == "1700000000.000000"
    assert post.parent_channel_id is None
    assert post.turn_card_intent_id == intent_id


async def test_a_turn_post_must_name_its_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.channel_tidy import record_turn_post

    with pytest.raises(ValueError, match="must name its turn"):
        await record_turn_post(
            db_session_factory,
            tenant_id=uuid.uuid4(),
            platform="discord",
            ma_agent_id="ag_x",
            channel_id="444",
            message_id="1000",
            requester_platform_user_id="42",
            source="turn",
        )


async def test_a_reply_is_a_turns_posts_in_one_channel_or_a_deliveries_chunks_or_one_post(
    db_session: AsyncSession,
) -> None:
    """The group `list_reply_posts` returns, and what each scope of it leaves out."""
    tenant, other_tenant = await make_tenant(db_session), await make_tenant(db_session)
    agent, other_agent = uuid.uuid4(), uuid.uuid4()
    turn, delivery = uuid.uuid4(), uuid.uuid4()

    async def post(message_id: str, **fields: object) -> None:
        values: dict[str, object] = {
            "tenant_id": tenant.id,
            "platform": "teams",
            "channel_id": "c1",
            "agent_id": agent,
        } | fields
        await record_post(db_session, message_id=message_id, **values)  # pyright: ignore[reportArgumentType]

    await post("t1", source="turn", turn_card_intent_id=turn)
    await post("t2", source="turn", turn_card_intent_id=turn)
    await post("t-deleted", source="turn", turn_card_intent_id=turn)
    await post("t-thread", source="turn", turn_card_intent_id=turn, kind="thread")
    await post("t-elsewhere", source="turn", turn_card_intent_id=turn, channel_id="c2")
    await post("t-platform", source="turn", turn_card_intent_id=turn, platform="discord")
    await post("t-agent", source="turn", turn_card_intent_id=turn, agent_id=other_agent)
    await post("t-tenant", source="turn", turn_card_intent_id=turn, tenant_id=other_tenant.id)
    await post("r1", source="routine", delivery_id=delivery, channel_id="c1;1")
    await post("r2", source="routine", delivery_id=delivery, channel_id="c1;2")
    await post("solo")
    await post("solo-neighbour")
    gone = await get_post(
        db_session, tenant_id=tenant.id, platform="teams", channel_id="c1", message_id="t-deleted"
    )
    assert gone is not None
    await mark_deleted(db_session, post_ids=[gone.id], now=datetime.now(UTC))

    async def reply_of(message_id: str, channel_id: str = "c1") -> list[str]:
        row = await get_post(
            db_session,
            tenant_id=tenant.id,
            platform="teams",
            channel_id=channel_id,
            message_id=message_id,
        )
        assert row is not None
        return sorted(p.message_id for p in await list_reply_posts(db_session, post=row))

    assert await reply_of("t1") == ["t1", "t2"], (
        "a turn's reply is its live messages in that channel, for this tenant, platform and agent"
        " alone, without its thread rows or deleted ones"
    )
    assert await reply_of("r1", "c1;1") == ["r1", "r2"], "a delivery spans its chunks' threads"
    assert await reply_of("solo") == ["solo"], "a post with neither is a reply of its own"
