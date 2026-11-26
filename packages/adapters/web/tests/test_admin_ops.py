"""`/api/admin/overview` and `/api/admin/issues`: the grouped failures an admin reads, the
resolve and mute buttons and their audit rows, and the walls between workspaces."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from daimon.core.ops_events import OpsEvent, record_of
from daimon.core.stores.ops_events import record_ops_events
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import funded_workspace
from .conftest import WebClientFactory
from .test_admin_routes import signed_in_admin


async def _report(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
    reason: str = "timeout",
    ago: timedelta = timedelta(minutes=5),
) -> str:
    """Store one failed-answer event and return its issue's fingerprint."""
    record = record_of(
        OpsEvent(
            code="turn.failed",
            tenant_id=tenant_id,
            reason=reason,
            agent_id="agent-1",
            account_id=account_id,
            platform="web",
        ),
        at=datetime.now(UTC) - ago,
    )
    async with factory.begin() as db:
        await record_ops_events(db, tenant_id=tenant_id, events=[record])
    return record.fingerprint


async def test_issues_group_events_and_show_people_and_events(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with signed_in_admin(web_client, db_session_factory, tmp_path) as admin:
        person = admin.ws.person
        fingerprint = await _report(
            db_session_factory, admin.ws.tenant_id, account_id=person.account_id
        )
        await _report(db_session_factory, admin.ws.tenant_id, ago=timedelta(minutes=1))
        other = await _report(db_session_factory, admin.ws.tenant_id, reason="upstream")
        listed = (await admin.client.get("/api/admin/issues")).json()
        detail = (await admin.client.get(f"/api/admin/issues/{fingerprint}")).json()
        events = (await admin.client.get(f"/api/admin/issues/{fingerprint}/events")).json()
        busiest = (await admin.client.get("/api/admin/issues?sort=events")).json()

    by_print = {i["fingerprint"]: i for i in listed}
    assert set(by_print) == {fingerprint, other}, "one issue per fingerprint"
    assert by_print[fingerprint]["events"] == 2, "its two events add up"
    assert by_print[fingerprint]["title"] == "Answer failed", "the title is in plain words"
    assert by_print[fingerprint]["reason_label"] == "Timed out"
    assert by_print[fingerprint]["state"] == "open" and by_print[fingerprint]["new"]
    assert sum(by_print[fingerprint]["buckets"]) == 2, "the trend counts the same events"
    assert detail["people"] == [{"id": str(person.id), "name": "Ada"}], "people are named"
    assert [e["reason_label"] for e in events] == ["Timed out", "Timed out"], "newest first"
    assert busiest[0]["fingerprint"] == fingerprint, "sort=events puts the busiest first"


async def test_resolve_mute_and_reopen_move_an_issue_and_leave_audit_rows(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with signed_in_admin(web_client, db_session_factory, tmp_path) as admin:
        fingerprint = await _report(db_session_factory, admin.ws.tenant_id)
        url = f"/api/admin/issues/{fingerprint}/state"
        muted = (await admin.client.post(url, json={"action": "mute"})).json()
        active = (await admin.client.get("/api/admin/issues")).json()
        in_muted = (await admin.client.get("/api/admin/issues?status=muted")).json()
        reopened = (await admin.client.post(url, json={"action": "reopen"})).json()
        resolved = (await admin.client.post(url, json={"action": "resolve"})).json()
        await _report(db_session_factory, admin.ws.tenant_id, ago=timedelta(seconds=-30))
        back = (await admin.client.get("/api/admin/issues")).json()
        trail = await admin.trail(db_session_factory)

    assert muted["state"] == "muted" and muted["state_by"]["name"] == "Ada"
    assert active == [], "a muted issue leaves the active list"
    assert [i["fingerprint"] for i in in_muted] == [fingerprint], "and shows under muted"
    assert reopened["state"] == "open" and reopened["state_by"] is None
    assert resolved["state"] == "resolved"
    assert back[0]["state"] == "open" and back[0]["regressed"], "a newer event reopens a resolve"
    assert [action for action, _, _ in trail] == [
        "Muted issue",
        "Reopened issue",
        "Resolved issue",
    ], "each change is audited"


async def test_an_issue_of_another_workspace_is_invisible_and_unchangeable(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with signed_in_admin(web_client, db_session_factory, tmp_path) as admin:
        elsewhere = await funded_workspace(db_session_factory)
        theirs = await _report(db_session_factory, elsewhere.tenant_id, reason="other-tenant")
        mine = await _report(db_session_factory, admin.ws.tenant_id)
        listed = (await admin.client.get("/api/admin/issues?period=30d&status=all")).json()
        detail = await admin.client.get(f"/api/admin/issues/{theirs}")
        events = await admin.client.get(f"/api/admin/issues/{theirs}/events")
        muted = await admin.client.post(
            f"/api/admin/issues/{theirs}/state", json={"action": "mute"}
        )
        overview = (await admin.client.get("/api/admin/overview")).json()

    assert [i["fingerprint"] for i in listed] == [mine], "only this workspace's issues list"
    assert (detail.status_code, events.status_code, muted.status_code) == (404, 404, 404)
    assert [i["fingerprint"] for i in overview["attention"]] == [mine]
    assert overview["status"] == "degraded", "an error seen in the last hour"


async def test_the_overview_has_the_contract_shape(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with signed_in_admin(web_client, db_session_factory, tmp_path) as admin:
        body = (await admin.client.get("/api/admin/overview?period=7d")).json()

    assert body["status"] == "healthy", "nothing failed"
    assert len(body["series"]) == 7, "one point per day"
    assert [a["area"] for a in body["by_area"]] == [
        "turn",
        "dm",
        "routine",
        "delivery",
        "watch",
        "wake",
        "bridge",
    ], "every area, in a fixed order"
    assert body["by_area"][0]["label"] == "Answers"
    assert body["workspace"]["funding_mode"] == "prepaid"
    assert body["attention"] == [] and body["failed"] == 0
