"""Monitored agents: what the switches keep, from when, and how the rows leave.

Real Postgres: the rule (`daimon.core.monitoring`), the store, the retention sweep and
account erasure. `test_monitoring_paths.py` checks that every chat and MCP path calls it.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core import monitoring, monitoring_view
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.monitoring import (
    capture_answer,
    capture_question,
    capture_tool_call,
    monitoring_state,
)
from daimon.core.monitoring_sweep import sweep_monitored_events
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_account
from daimon.core.stores import monitoring as store
from daimon.core.stores.domain import (
    FeatureTarget,
    FeatureValueRow,
    MonitoredEventRow,
    MonitoredPlatform,
)
from daimon.core.stores.feature_values import set_feature_value
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]


async def _switch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: str,
    value: str = "on",
    *,
    agent: str | None = None,
) -> FeatureValueRow:
    target = FeatureTarget("agent", agent_id=agent) if agent else FeatureTarget("workspace")
    row = await set_feature_value(
        session,
        tenant_id=tenant_id,
        target=target,
        key=key,
        value=value,
        set_by=None,
        surface="web",
    )
    await session.commit()
    return row


async def _ask(
    sessions: Sessions,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    agent: str = "agt_a",
    platform: MonitoredPlatform = "discord",
    conversation: str = "dm-1",
    turn_ref: str = "t1",
) -> bool:
    kept = await capture_question(
        sessions,
        None,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent),
        platform=platform,
        account_id=account_id,
        conversation=conversation,
        turn_ref=turn_ref,
        content=f"question {turn_ref}",
    )
    return kept is not None


async def _answer(sessions: Sessions, tenant_id: uuid.UUID, turn_ref: str) -> bool:
    return await capture_answer(
        sessions,
        tenant_id=tenant_id,
        conversation="dm-1",
        turn_ref=turn_ref,
        content=f"answer {turn_ref}",
        outcome="completed",
    )


async def _rows(
    sessions: Sessions, tenant_id: uuid.UUID, agent: str = "agt_a"
) -> list[MonitoredEventRow]:
    async with sessions() as session:
        return await store.list_events(
            session,
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent),
        )


async def test_an_agent_nobody_monitors_keeps_nothing(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Both switches are off by default, so a question and its answer leave no row."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    assert not await _ask(db_session_factory, tenant.id, account.id), "off must keep nothing"
    assert not await _answer(db_session_factory, tenant.id, "t1"), "no question, no answer"
    assert await _rows(db_session_factory, tenant.id) == [], "no row may exist"


async def test_an_agent_switch_monitors_only_that_agent(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """An agent-tier row is matched by the agent's derived id, never another agent's."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_direct_messages", agent="agt_a")

    assert await _ask(db_session_factory, tenant.id, account.id), "agt_a is monitored"
    assert not await _ask(
        db_session_factory, tenant.id, account.id, agent="agt_b", turn_ref="t2"
    ), "agt_b is not monitored"
    assert [row.turn_ref for row in await _rows(db_session_factory, tenant.id)] == ["t1"]
    assert await _rows(db_session_factory, tenant.id, "agt_b") == [], "agt_b kept a row"


async def test_a_workspace_switch_monitors_every_agent_since_it_was_set(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """The workspace row covers each agent, its `set_at` is the start, and an agent row
    turning one agent off wins over it."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await _switch(db_session, tenant.id, "monitor_direct_messages")
    await _switch(db_session, tenant.id, "monitor_direct_messages", "off", agent="agt_c")

    for agent in ("agt_a", "agt_b"):
        async with db_session_factory() as session:
            state = await monitoring_state(
                session,
                None,
                tenant_id=tenant.id,
                agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent),
                platform="slack",
            )
        assert state.on and state.since == row.set_at, f"{agent}: {state}"
    assert not await _ask(db_session_factory, tenant.id, account.id, agent="agt_c"), (
        "the agent's own off must beat the workspace's on"
    )


async def test_a_conversation_begun_before_the_switch_is_kept_from_then_on(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """A thread that existed before the switch keeps only turns asked after it; the
    earlier turn's answer is not kept, and each answer is kept once."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    assert not await _ask(db_session_factory, tenant.id, account.id, turn_ref="before")
    await _switch(db_session, tenant.id, "monitor_direct_messages", agent="agt_a")
    assert not await _answer(db_session_factory, tenant.id, "before"), (
        "a turn asked before the switch has no kept question, so no kept answer"
    )
    assert await _ask(db_session_factory, tenant.id, account.id, turn_ref="after")
    assert await monitoring.open_questions(
        db_session_factory, tenant_id=tenant.id, conversation="dm-1"
    ) == {"after"}
    assert await _answer(db_session_factory, tenant.id, "after"), "the answer is kept"
    assert not await _answer(db_session_factory, tenant.id, "after"), "kept twice"

    rows = await _rows(db_session_factory, tenant.id)
    assert [(row.kind, row.turn_ref) for row in rows] == [
        ("answer", "after"),
        ("question", "after"),
    ], rows
    assert rows[0].account_id == account.id and rows[0].platform == "discord"


async def test_the_dm_and_mcp_switches_are_separate(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """`monitor_mcp` keeps MCP turns only; direct messages need their own switch."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_mcp", agent="agt_a")

    assert await _ask(db_session_factory, tenant.id, account.id, platform="mcp"), "mcp kept"
    for platform in ("discord", "slack", "teams", "web"):
        assert not await _ask(
            db_session_factory, tenant.id, account.id, platform=platform, turn_ref=platform
        ), f"{platform} was kept under monitor_mcp"


async def test_a_tool_call_keeps_its_name_time_and_outcome_only(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """An MCP tool call row has no content and no conversation: nothing it sent or got."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_mcp", agent="agt_a")
    at = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)

    kept = await capture_tool_call(
        db_session_factory,
        None,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
        account_id=account.id,
        tool_name="notion_search",
        outcome="error",
        at=at,
    )

    assert kept, "a monitored agent's tool call is kept"
    [row] = await _rows(db_session_factory, tenant.id)
    assert (row.kind, row.tool_name, row.outcome, row.created_at) == (
        "tool",
        "notion_search",
        "error",
        at,
    ), row
    assert row.content is None and row.conversation is None and row.turn_ref is None, row


async def test_a_tool_day_transcript_holds_that_utc_day_only(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """A tool-call day reads by a `created_at` range: both ends of the day, not the next."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_mcp", agent="agt_a")
    day = datetime(2026, 11, 1, tzinfo=UTC)
    for at in (day, day + timedelta(hours=23, minutes=59), day + timedelta(days=1)):
        await capture_tool_call(
            db_session_factory,
            None,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
            account_id=account.id,
            tool_name="notion_search",
            outcome="completed",
            at=at,
        )

    async with db_session_factory() as session:
        rows = await monitoring_view.list_conversations(
            session, tenant_id=tenant.id, ma_agent_id="agt_a"
        )
        [first] = [row for row in rows if row.day == "2026-11-01"]
        events = await monitoring_view.get_transcript(
            session,
            tenant_id=tenant.id,
            ma_agent_id="agt_a",
            conversation=monitoring_view.conversation_id(first),
        )
        bad = base64.urlsafe_b64encode(f"{account.id}|tools:nope".encode()).decode()
        refused = await monitoring_view.get_transcript(
            session, tenant_id=tenant.id, ma_agent_id="agt_a", conversation=bad
        )
    assert events is not None and len(events) == 2, events
    assert refused is None, "a tool day that is not a date is not a handle"


async def test_a_failed_write_never_fails_the_caller(
    db_session: AsyncSession,
    db_session_factory: Sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capture is a boundary: a store error returns False instead of raising."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_direct_messages", agent="agt_a")

    async def broken(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "record_events", broken)
    assert not await _ask(db_session_factory, tenant.id, account.id), "must swallow the error"


async def test_list_events_pages_newest_first_with_filters(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Keyset paging returns each row once, newest first; filters narrow by platform."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a")
    start = datetime(2026, 11, 1, tzinfo=UTC)
    async with db_session_factory() as session, session.begin():
        for index, platform in enumerate(("discord", "web", "discord", "mcp", "discord")):
            subject = store.MonitoredSubject(tenant.id, agent, platform, account.id, "c")
            entry = store.MonitoredEntry(
                "question",
                content=str(index),
                turn_ref=str(index),
                created_at=start + timedelta(minutes=index),
            )
            await store.record_events(session, subject, (entry,))

    seen: list[str | None] = []
    before = None
    async with db_session_factory() as session:
        while page := await store.list_events(
            session, tenant_id=tenant.id, agent_id=agent, before=before, limit=2
        ):
            seen += [row.content for row in page]
            before = (page[-1].created_at, page[-1].id)
        discord = await store.list_events(
            session, tenant_id=tenant.id, agent_id=agent, platforms=("discord",)
        )
        since = await store.count_events(
            session, tenant_id=tenant.id, agent_id=agent, since=start + timedelta(minutes=3)
        )
    assert seen == ["4", "3", "2", "1", "0"], seen
    assert [row.content for row in discord] == ["4", "2", "0"], discord
    assert since == 2, since


async def test_the_sweep_prunes_only_rows_past_retention(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Rows older than the retention period go, in batches; newer rows stay."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a")
    now = datetime(2026, 11, 17, tzinfo=UTC)
    async with db_session_factory() as session, session.begin():
        subject = store.MonitoredSubject(tenant.id, agent, "web", account.id, "c")
        entries = [
            store.MonitoredEntry("question", content=str(days), created_at=now - timedelta(days))
            for days in (120, 91, 89, 1)
        ]
        await store.record_events(session, subject, entries)

    pruned = await sweep_monitored_events(db_session_factory, now=now, retention_days=90, batch=1)

    assert pruned == 2, pruned
    rows = await _rows(db_session_factory, tenant.id)
    assert [row.content for row in rows] == ["1", "89"], rows


async def test_account_erasure_previews_and_deletes_the_persons_rows(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """The privacy preview counts what the purge deletes; another person's rows stay."""
    tenant = await make_tenant(db_session)
    first = await make_account(db_session, tenant=tenant)
    second = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_direct_messages", agent="agt_a")
    assert await _ask(db_session_factory, tenant.id, first.id, turn_ref="a")
    assert await _answer(db_session_factory, tenant.id, "a")
    assert await _ask(db_session_factory, tenant.id, second.id, turn_ref="b")

    preview = await collect_purge_preview(sm=db_session_factory, account_id=first.id)
    report = await purge_account(sm=db_session_factory, account_id=first.id)

    assert preview.monitored_events.count == 2, preview.monitored_events
    assert report.db.monitored_events == 2, report.db
    rows = await _rows(db_session_factory, tenant.id)
    assert [row.account_id for row in rows] == [second.id], rows
