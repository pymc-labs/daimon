import uuid
from datetime import UTC, date, datetime, timedelta

from daimon.core.audit_trail import AuditFilter, day_bounds, list_entries, list_kinds
from daimon.core.stores import security_audit, web_audit
from daimon.testing.factories import make_account, make_tenant

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


async def _web(session, tenant, *, minutes, action="Suspended", actor=None):
    await web_audit.record(
        session,
        tenant_id=tenant.id,
        actor_account_id=actor,
        action=action,
        target="Mia",
        at=T0 + timedelta(minutes=minutes),
    )


async def _event(session, tenant, *, minutes, tool="panel:agent_backend", account=None):
    await security_audit.append_event(
        session,
        tenant_id=tenant.id,
        account_id=account,
        agent_id=None,
        platform="discord",
        platform_user_id="1",
        tool_name=tool,
        operation="x",
        outcome="allowed",
        reason="completed",
        occurred_at=T0 + timedelta(minutes=minutes),
    )


async def test_both_sources_merge_newest_first_and_page_without_gaps(db_session):
    tenant = await make_tenant(db_session)
    await _web(db_session, tenant, minutes=1)
    await _event(db_session, tenant, minutes=2)
    await _web(db_session, tenant, minutes=3)
    await _event(db_session, tenant, minutes=4)

    everything = await list_entries(db_session, tenant_id=tenant.id)
    first = await list_entries(db_session, tenant_id=tenant.id, limit=3)
    rest = await list_entries(db_session, tenant_id=tenant.id, before=first[-1].cursor, limit=3)

    assert [e.source for e in everything] == ["security", "web", "security", "web"]
    assert first + rest == everything, "a cursor from the merged page resumes every source"


async def test_filters_narrow_by_source_actor_kind_and_dates(db_session):
    tenant = await make_tenant(db_session)
    ada = await make_account(db_session, tenant=tenant)
    await _web(db_session, tenant, minutes=0, actor=ada.id)
    await _web(db_session, tenant, minutes=1, action="Restored")
    await _event(db_session, tenant, minutes=2, account=ada.id)
    await _event(db_session, tenant, minutes=3, tool="channel_backend")

    async def kinds(**f):
        rows = await list_entries(db_session, tenant_id=tenant.id, query=AuditFilter(**f))
        return sorted(e.kind for e in rows)

    assert await kinds(source="web") == ["Restored", "Suspended"]
    assert await kinds(source="security") == ["channel_backend", "panel:agent_backend"]
    assert await kinds(actor_account_id=ada.id) == ["Suspended", "panel:agent_backend"]
    assert await kinds(kind="Restored") == ["Restored"]
    assert await kinds(kind="channel_backend") == ["channel_backend"]
    assert await kinds(since=T0 + timedelta(minutes=2)) == [
        "channel_backend",
        "panel:agent_backend",
    ]
    assert await kinds(until=T0 + timedelta(minutes=1)) == ["Suspended"], "until is exclusive"


async def test_the_trail_holds_one_workspace_only(db_session):
    mine, other = await make_tenant(db_session), await make_tenant(db_session)
    await _web(db_session, other, minutes=0)
    await _event(db_session, other, minutes=0)

    assert await list_entries(db_session, tenant_id=mine.id) == []
    assert await list_kinds(db_session, tenant_id=mine.id) == []


async def test_kinds_name_each_source_once(db_session):
    tenant = await make_tenant(db_session)
    for minutes in (0, 1):
        await _web(db_session, tenant, minutes=minutes)
        await _event(db_session, tenant, minutes=minutes)

    assert [(k.source, k.kind) for k in await list_kinds(db_session, tenant_id=tenant.id)] == [
        ("web", "Suspended"),
        ("security", "panel:agent_backend"),
    ]


def test_day_bounds_cover_both_days_in_utc():
    since, until = day_bounds(date(2026, 3, 10), date(2026, 3, 11))

    assert since == datetime(2026, 3, 10, tzinfo=UTC)
    assert until == datetime(2026, 3, 12, tzinfo=UTC)
    assert day_bounds(None, None) == (None, None)


async def test_a_tie_on_time_still_pages_each_row_once(db_session):
    tenant = await make_tenant(db_session)
    for _ in range(3):
        await _web(db_session, tenant, minutes=0)
        await _event(db_session, tenant, minutes=0)

    seen: list[uuid.UUID] = []
    before = None
    while page := await list_entries(db_session, tenant_id=tenant.id, before=before, limit=2):
        seen += [e.id for e in page]
        before = page[-1].cursor

    assert len(seen) == len(set(seen)) == 6
