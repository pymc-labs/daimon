"""Monitored agents: what the switches keep, from when, and how the rows leave.

Real Postgres: the rule (`daimon.core.monitoring`), the store, the retention sweep and
account erasure. `test_monitoring_paths.py` checks that every chat and MCP path calls it.
"""

from __future__ import annotations

import base64
import functools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core import monitoring, monitoring_view
from daimon.core.admin_actions import DelegatedAction, admin_for_action
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import FeatureActor, may_change
from daimon.core.features.snapshot import CACHE
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.monitoring import (
    MCP_NOTICE,
    capture_answer,
    capture_question,
    capture_tool_call,
    may_monitor,
    mcp_notice_due,
    monitoring_state,
    record_notice,
)
from daimon.core.monitoring_sweep import sweep_monitored_events
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_account
from daimon.core.stores import agent_owners, web_roles
from daimon.core.stores import monitoring as store
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import (
    FeatureTarget,
    FeatureValueRow,
    MonitoredEventRow,
    MonitoredPlatform,
    Role,
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
    CACHE.clear()  # As `change_feature` does.
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
    told: bool = False,
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
    if told and kept is not None and kept.notice is not None:
        assert await record_notice(sessions, kept.subject, kept.notice, turn_ref=turn_ref)
    return kept is not None


async def _tell_over_mcp(sessions: Sessions, tenant_id: uuid.UUID, account_id: uuid.UUID) -> None:
    agent = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agt_a")
    subject = store.MonitoredSubject(tenant_id, agent, "mcp", account_id, None)
    assert await record_notice(sessions, subject, MCP_NOTICE)


async def _tool(
    sessions: Sessions, tenant_id: uuid.UUID, account_id: uuid.UUID, *, at: datetime | None = None
) -> bool:
    return await capture_tool_call(
        sessions,
        None,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agt_a"),
        account_id=account_id,
        tool_name="notion_search",
        outcome="error",
        at=at,
    )


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


async def test_back_on_at_the_workspace_starts_a_new_since(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Off at the workspace, then on again there: the agent row decides once more, but
    `since` is the workspace's change, so people are told again."""
    tenant = await make_tenant(db_session)
    agent_row = await _switch(db_session, tenant.id, "monitor_direct_messages", agent="agt_a")

    async def state() -> monitoring.MonitoringState:
        async with db_session_factory() as session:
            return await monitoring_state(
                session,
                None,
                tenant_id=tenant.id,
                agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
                platform="discord",
            )

    assert (await state()).since == agent_row.set_at
    await _switch(db_session, tenant.id, "monitor_direct_messages", "off")
    assert not (await state()).on, "the workspace's off caps the agent's on"
    back = await _switch(db_session, tenant.id, "monitor_direct_messages")
    now = await state()
    assert now.feature.tier == "agent" and now.since == back.set_at > agent_row.set_at, now


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
    await _tell_over_mcp(db_session_factory, tenant.id, account.id)

    assert await _tool(db_session_factory, tenant.id, account.id, at=at), "kept once told"
    [row] = [row for row in await _rows(db_session_factory, tenant.id) if row.kind == "tool"]
    assert (row.kind, row.tool_name, row.outcome, row.created_at) == (
        "tool",
        "notion_search",
        "error",
        at,
    ), row
    assert row.content is None and row.conversation is None and row.turn_ref is None, row


async def test_may_monitor_holds_its_answer_until_a_feature_changes(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """The hot path's memo: a store write alone leaves it; a feature change drops it."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    memo = functools.partial(
        may_monitor, db_session_factory, None, tenant_id=tenant.id, platform="discord"
    )

    assert not await memo(), "nothing is monitored yet"
    await set_feature_value(
        db_session,
        tenant_id=tenant.id,
        target=FeatureTarget("agent", agent_id="agt_a"),
        key="monitor_direct_messages",
        value="on",
        set_by=None,
        surface="web",
    )
    await db_session.commit()
    assert not await memo(), "held until a feature change"
    CACHE.clear()
    assert await memo(), "a feature change drops the memo"
    assert not await may_monitor(db_session_factory, None, tenant_id=tenant.id, platform="mcp"), (
        "per platform"
    )


async def test_a_tool_call_is_kept_only_once_the_person_was_told(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Until a notice row since the switch on, the call's result must carry the notice."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_mcp", agent="agt_a")
    agent = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a")
    due = functools.partial(
        mcp_notice_due, db_session_factory, None, tenant_id=tenant.id, agent_id=agent
    )

    assert await due(account_id=account.id), "an untold person is due the notice"
    assert not await _tool(db_session_factory, tenant.id, account.id), "kept untold"
    await _tell_over_mcp(db_session_factory, tenant.id, account.id)
    assert not await due(account_id=account.id), "told once per switch on"
    assert await _tool(db_session_factory, tenant.id, account.id), "told, the call is kept"


async def test_a_tool_day_transcript_holds_that_utc_day_only(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """A tool-call day reads by a `created_at` range: both ends of the day, not the next."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_mcp", agent="agt_a")
    day = datetime(2026, 11, 1, tzinfo=UTC)
    await _tell_over_mcp(db_session_factory, tenant.id, account.id)
    for at in (day, day + timedelta(hours=23, minutes=59), day + timedelta(days=1)):
        assert await _tool(db_session_factory, tenant.id, account.id, at=at)

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


async def test_a_notice_is_one_transcript_row_never_a_conversation_alone(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_direct_messages", agent="agt_a")
    assert await _ask(db_session_factory, tenant.id, account.id, told=True)
    agent = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a")
    alone = store.MonitoredSubject(tenant.id, agent, "discord", account.id, "dm-2")
    assert await record_notice(db_session_factory, alone, monitoring.CHAT_NOTICE)

    async with db_session_factory() as session:
        listed = await store.list_conversations(session, tenant_id=tenant.id, agent_id=agent)
        thread = await store.list_events(
            session, tenant_id=tenant.id, agent_id=agent, conversation="dm-1"
        )
    assert [(c.conversation, c.questions) for c in listed] == [("dm-1", 1)], listed
    assert sorted(row.kind for row in thread) == ["notice", "question"], thread


async def test_account_erasure_previews_and_deletes_the_persons_rows(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """The privacy preview counts what the purge deletes; another person's rows stay."""
    tenant = await make_tenant(db_session)
    first = await make_account(db_session, tenant=tenant)
    second = await make_account(db_session, tenant=tenant)
    await _switch(db_session, tenant.id, "monitor_direct_messages", agent="agt_a")
    assert await _ask(db_session_factory, tenant.id, first.id, turn_ref="a", told=True)
    assert await _answer(db_session_factory, tenant.id, "a")
    assert await _ask(db_session_factory, tenant.id, second.id, turn_ref="b")

    preview = await collect_purge_preview(sm=db_session_factory, account_id=first.id)
    report = await purge_account(sm=db_session_factory, account_id=first.id)

    assert preview.monitored_events.count == 3, preview.monitored_events
    assert report.db.monitored_events == 3, report.db
    rows = await _rows(db_session_factory, tenant.id)
    assert [row.account_id for row in rows] == [second.id], rows


async def test_workspace_admins_switch_monitoring_but_an_agent_owner_does_not() -> None:
    """The switches use the workspace-admin authority and drop the owner path, as long jobs do."""

    async def admin_for(action: DelegatedAction | None) -> bool:
        return admin_for_action(platform_admin=False, daimon_admin=True, action=action)

    async def owns(_: str) -> bool:
        return True

    admin = FeatureActor(uuid.uuid4(), None, "chat", None, admin_for)
    owner = FeatureActor(uuid.uuid4(), None, "chat", None, _never, owns_agent=owns)
    for key in ("monitor_direct_messages", "monitor_mcp"):
        for tier in ("workspace", "agent"):
            spec = FEATURES[key]
            assert await may_change(admin, spec, tier, locked=False, agent_id="ag"), (
                f"a workspace admin sets {key} at the {tier} tier"
            )
            assert not await may_change(owner, spec, tier, locked=False, agent_id="ag"), (
                f"an agent's owner does not set {key} at the {tier} tier"
            )


async def _never(_action: object) -> bool:
    return False


async def test_may_read_monitored_is_a_workspace_owner_or_admin_only(
    db_session: AsyncSession,
) -> None:
    """One rule for every read: grants and a platform admin pass; an agent's owner, a plain
    member and a stranger from another workspace do not."""

    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    owner, admin, platform, member, agent_owner, chat = [
        await make_account(db_session, tenant=tenant) for _ in range(6)
    ]
    for account, role in ((owner, "owner"), (admin, "admin")):
        await web_roles.set_workspace_role(
            db_session,
            tenant_id=tenant.id,
            account_id=account.id,
            role=role,  # pyright: ignore[reportArgumentType]
            granted_by_account_id=None,
        )
    await set_role(db_session, platform.id, Role.ADMIN)
    await agent_owners.set_owner(
        db_session,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        owner_account_id=agent_owner.id,
        source="created",
    )

    async def reads(account: uuid.UUID, tenant_id: uuid.UUID, **kw: bool) -> bool:
        return await monitoring_view.may_read_monitored(
            db_session, tenant_id=tenant_id, account_id=account, **kw
        )

    assert [await reads(a.id, tenant.id) for a in (owner, admin, platform)] == [True] * 3, (
        "an owner grant, an admin grant and a platform admin read"
    )
    assert [await reads(a.id, tenant.id) for a in (member, agent_owner)] == [False] * 2, (
        "a member and an agent's owner who holds no grant do not read"
    )
    assert await reads(chat.id, tenant.id, chat_admin=True), (
        "a chat admin without a web person reads"
    )
    assert not await reads(owner.id, other.id), "a grant in one workspace is nothing in another"
