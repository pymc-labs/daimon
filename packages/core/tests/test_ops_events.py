"""Ops events: what is stored, how it is grouped into issues, and who can see it.

An issue is the events of one fingerprint. These tests keep to the rules that must hold:
a workspace never reads another's events, repeats coalesce into one row, nothing but a
closed vocabulary is stored, a resolved issue comes back when it recurs, and retention and a
person's purge remove the rows.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core import ops_events
from daimon.core.ops_catalog import clean_note, clean_reason, fingerprint_of
from daimon.core.ops_events import OpsEvent, drain_ops_events, record_of, report_ops_event
from daimon.core.ops_events_sweep import sweep_ops_events
from daimon.core.ops_overview import (
    attention_of,
    parse_window,
    state_of,
    status_of,
)
from daimon.core.stores import ops_events as store
from daimon.core.stores.domain import OpsIssueGroupRow, OpsIssueStateRow
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _event(tenant_id: uuid.UUID | None, **fields: object) -> OpsEvent:
    base: dict[str, object] = {"reason": "timeout", "agent_id": "agent-1", "platform": "web"}
    return OpsEvent("turn.failed", tenant_id, **{**base, **fields})  # pyright: ignore[reportArgumentType]


def _group(
    *, last_seen: datetime, severity: str = "error", state: str | None = None, people: int = 1
) -> OpsIssueGroupRow:
    record = record_of(_event(uuid.uuid4()), at=last_seen)
    latest = {**record.__dict__, "id": uuid.uuid4(), "severity": severity}
    held = (
        OpsIssueStateRow(fingerprint=record.fingerprint, state=state, by_account_id=None, at=NOW)  # pyright: ignore[reportArgumentType]
        if state
        else None
    )
    return OpsIssueGroupRow.model_validate(
        {
            "fingerprint": record.fingerprint,
            "events": 1,
            "people": people,
            "first_seen": last_seen,
            "last_seen": last_seen,
            "latest": latest,
            "state": held,
        }
    )


def test_a_reason_is_one_token_and_anything_else_is_other() -> None:
    assert clean_reason("timeout") == "timeout" and clean_reason("502") == "502"
    assert clean_reason("could not connect to db at 10.0.0.1") == "other", "no sentence is kept"
    assert clean_reason(None) is None
    assert clean_note("  one\n two  ") == "one two", "a note is one line"
    assert len(clean_note("x" * 500) or "") == 300, "and is cut to its limit"


def test_a_fingerprint_names_the_issue_and_a_person_only_where_the_code_says() -> None:
    a = fingerprint_of("turn.failed", "turn", "agent-1", "timeout", "p1")
    assert a == fingerprint_of("turn.failed", "turn", "agent-1", "timeout", "p2"), "not per person"
    assert a != fingerprint_of("turn.failed", "turn", "agent-1", "upstream", None), "per reason"
    reconnect = [
        fingerprint_of("connector.reconnect_needed", "bridge", "drive", "expired", person)
        for person in ("p1", "p2")
    ]
    assert reconnect[0] != reconnect[1], "a connector needing a sign-in is one issue per person"
    assert uuid.UUID(a), "a valid UUID string, so it pages like any keyset cursor"


def test_a_resolved_issue_returns_when_it_recurs_and_a_muted_one_does_not() -> None:
    quiet = _group(last_seen=NOW - timedelta(hours=1), state="resolved")
    assert state_of(quiet) == ("resolved", False), "resolved after its last event"
    again = _group(last_seen=NOW + timedelta(minutes=1), state="resolved")
    assert state_of(again) == ("open", True), "a newer event reopens it, marked regressed"
    muted = _group(last_seen=NOW + timedelta(minutes=1), state="muted")
    assert state_of(muted) == ("muted", False), "muted stays hidden whatever arrives"


def test_status_is_failing_degraded_or_healthy_from_the_last_hour() -> None:
    error = _group(last_seen=NOW)
    assert status_of(answers=100, failed=10, hour=[]) == "healthy"
    assert status_of(answers=100, failed=21, hour=[]) == "failing", "over 20% of 20+ answers"
    assert status_of(answers=10, failed=9, hour=[]) == "healthy", "too few answers to judge"
    assert status_of(answers=0, failed=0, hour=[error]) == "degraded", "an open error issue"
    assert (
        status_of(answers=0, failed=0, hour=[_group(last_seen=NOW, severity="warning")])
        == "healthy"
    )
    assert status_of(answers=0, failed=0, hour=[_group(last_seen=NOW, people=3)]) == "failing"
    muted = _group(last_seen=NOW, state="muted", people=5)
    assert status_of(answers=0, failed=0, hour=[muted]) == "healthy", "a muted issue is not counted"


def test_windows_parse_and_attention_lists_errors_first() -> None:
    assert parse_window("15m") == timedelta(minutes=15) and parse_window("7d") == timedelta(days=7)
    with pytest.raises(ValueError, match="window"):
        parse_window("soon")
    assert attention_of([]) == [], "nothing needs attention"


async def test_a_workspace_reads_only_its_own_events_and_states(db_session: AsyncSession) -> None:
    mine, theirs = await make_tenant(db_session), await make_tenant(db_session)
    record = record_of(_event(mine.id), at=NOW)
    await store.record_ops_events(db_session, tenant_id=mine.id, events=[record])
    await store.record_ops_events(db_session, tenant_id=theirs.id, events=[record])
    await store.set_issue_state(
        db_session,
        tenant_id=theirs.id,
        fingerprint=record.fingerprint,
        state="muted",
        by_account_id=None,
        at=NOW,
    )
    since = NOW - timedelta(days=1)

    groups = await store.list_issue_groups(db_session, tenant_id=mine.id, since=since)
    events = await store.list_issue_events(
        db_session, tenant_id=mine.id, fingerprint=record.fingerprint, before=None, limit=10
    )
    by_area = await store.count_by_area(db_session, tenant_id=mine.id, since=since)

    assert [g.events for g in groups] == [1], "only this workspace's event is counted"
    assert groups[0].state is None, "the other workspace's mute is not mine"
    assert len(events) == 1 and by_area == {"turn": 1}, "events and totals the same"
    assert await store.list_issue_groups(db_session, tenant_id=uuid.uuid4(), since=since) == []


async def test_repeats_within_the_window_make_one_row_and_a_bad_reason_is_other(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    for _ in range(5):
        report_ops_event(sm, _event(tenant.id))
    report_ops_event(sm, _event(tenant.id, reason="boom: secret text here"))
    report_ops_event(sm, _event(None))
    await drain_ops_events()
    async with sm() as session:
        groups = await store.list_issue_groups(
            session, tenant_id=tenant.id, since=datetime.now(UTC) - timedelta(hours=1)
        )

    assert sorted(g.events for g in groups) == [1, 5], "five repeats share one row's count"
    assert {g.latest.reason for g in groups} == {"timeout", "other"}, "free text is not stored"


async def test_a_failed_turn_leaves_an_event_and_a_direct_message_is_its_own_area(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    TurnObservation(sm, tenant.id, "discord", is_dm=True).finish(reason=TerminationReason.UPSTREAM)
    TurnObservation(sm, tenant.id, "discord").finish(reason=TerminationReason.COMPLETED)
    await drain_outcomes()
    await drain_ops_events()
    async with sm() as session:
        groups = await store.list_issue_groups(
            session, tenant_id=tenant.id, since=datetime.now(UTC) - timedelta(hours=1)
        )

    assert [(g.latest.code, g.latest.area, g.latest.reason) for g in groups] == [
        ("turn.failed", "dm", "upstream")
    ], "only the failed turn, in the direct message area"


async def test_an_event_that_cannot_be_written_is_swallowed(
    db_session: AsyncSession, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    async def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("PRIVATE VALUE")

    monkeypatch.setattr(ops_events, "record_ops_events", broken)
    report_ops_event(async_sessionmaker(db_engine), _event(tenant.id, reason="upstream"))
    await asyncio.wait_for(drain_ops_events(), timeout=5)


async def test_retention_removes_old_events_and_only_orphaned_states(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    old, fresh = (
        record_of(_event(tenant.id, reason=reason), at=at)
        for reason, at in (("upstream", NOW - timedelta(days=40)), ("timeout", NOW))
    )
    await store.record_ops_events(db_session, tenant_id=tenant.id, events=[old, fresh])
    for record in (old, fresh):
        await store.set_issue_state(
            db_session,
            tenant_id=tenant.id,
            fingerprint=record.fingerprint,
            state="muted",
            by_account_id=None,
            at=NOW - timedelta(days=39),
        )
    await db_session.commit()

    removed = await sweep_ops_events(db_session_factory, now=NOW, retention_days=30, batch=1)
    groups = await store.list_issue_groups(
        db_session, tenant_id=tenant.id, since=NOW - timedelta(days=60)
    )

    assert removed == 1, "the 40-day-old event goes"
    assert [(g.fingerprint, g.state is not None) for g in groups] == [(fresh.fingerprint, True)], (
        "a mute on an issue still seen stays; the one on a vanished issue goes"
    )


async def test_a_persons_events_go_with_their_account(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    gone, kept = (
        await make_account(db_session, tenant=tenant),
        await make_account(db_session, tenant=tenant),
    )
    records = [
        record_of(_event(tenant.id, account_id=account.id), at=NOW) for account in (gone, kept)
    ]
    await store.record_ops_events(db_session, tenant_id=tenant.id, events=records)

    assert await store.count_for_account(db_session, account_id=gone.id) == 1
    assert await store.delete_for_account(db_session, account_id=gone.id) == 1
    assert await store.count_for_account(db_session, account_id=kept.id) == 1, "only theirs"
