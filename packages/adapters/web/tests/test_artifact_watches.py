"""The Activity tab's watch buttons: a manager pauses or deletes any watch on the page,
an owner their own, and only the owner resumes one."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.stores import watches
from daimon.core.stores.domain import ArtifactRow, WebPersonRow
from daimon.core.stores.identity import create_platform_principal
from daimon.testing.builders import new_thread_session
from daimon.testing.watch_fixtures import install_poll_ports
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from .agent_support import OWN
from .artifact_support import page_client, publish, team


async def _watch_of(
    factory: async_sessionmaker[AsyncSession], person: WebPersonRow, page: ArtifactRow
) -> uuid.UUID:
    """`person`'s watch on the page, made from a Discord thread of their linked account."""
    now, user = datetime.now(UTC), uuid.uuid4().hex[:8]
    async with factory.begin() as db:
        await create_platform_principal(
            db,
            tenant_id=person.tenant_id,
            platform="discord",
            external_id=user,
            account_id=person.account_id,
        )
        await new_thread_session(
            db,
            tenant_id=person.tenant_id,
            thread_id=f"t-{user}",
            channel_id="4241",
            account_id=person.account_id,
            ma_session_id=f"s-{user}",
            ma_agent_id="agent-bound",
        )
        binding = ContinuationRequest(
            tenant_id=person.tenant_id,
            platform="discord",
            parent_channel_id="4241",
            thread_id=f"t-{user}",
            requester_account_id=person.account_id,
            requester_external_user_id=user,
            target_ma_agent_id="agent-bound",
            target_name="analyst",
            requested_work=None,
            reason="watch_fired",
            idempotency_key=uuid.uuid4(),
        )
        watch = await watches.register_page_watch(
            db, binding, instructions="Tally.", page_id=page.id, expires_at=None, now=now
        )
    return watch.id


async def test_managers_pause_and_delete_and_only_owners_resume(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    async with db_session_factory.begin() as db:
        await install_poll_ports(db)
        await watches.set_watch_control(db, scope="global", enabled=True)
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, events=True, access="workspace")
    theirs = await _watch_of(db_session_factory, t.member, page)
    mine = await _watch_of(db_session_factory, t.owner, page)
    url = f"/api/artifacts/{page.id}/watches/"
    pause, resume = {"action": "pause"}, {"action": "resume"}

    async with page_client(db_session_factory, t.owner) as owner:
        listed = (await owner.get(f"/api/artifacts/{page.id}")).json()["watches"]
        assert {w["id"]: w["mine"] for w in listed} == {str(theirs): False, str(mine): True}
        assert (await owner.post(url + str(theirs), json=pause, headers=OWN)).status_code == 204
        assert (await owner.post(url + str(theirs), json=resume, headers=OWN)).status_code == 403
        assert (await owner.post(url + str(mine), json=pause, headers=OWN)).status_code == 204
        assert (await owner.post(url + str(mine), json=resume, headers=OWN)).status_code == 204
    async with page_client(db_session_factory, t.member) as member:
        assert (await member.post(url + str(mine), json=pause, headers=OWN)).status_code == 404
        assert (await member.post(url + str(theirs), json=resume, headers=OWN)).status_code == 204
        assert (await member.delete(url + str(theirs), headers=OWN)).status_code == 204
    async with db_session_factory() as db:
        kept = await watches.get_watch(db, mine)
        assert kept is not None and kept.status == "active", "resumed with a fresh key"
        assert await watches.get_watch(db, theirs) is None


async def test_a_delivery_the_database_refuses_is_logged_and_the_event_kept(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    async with db_session_factory.begin() as db:
        await install_poll_ports(db)
        await watches.set_watch_control(db, scope="global", enabled=True)
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, events=True, access="workspace")
    watch_id = await _watch_of(db_session_factory, t.owner, page)
    refused = DBAPIError("SELECT", None, Exception("cap"))
    monkeypatch.setattr(watches, "accept_page_event", AsyncMock(side_effect=refused))
    with capture_logs() as logs:
        async with page_client(db_session_factory, t.member) as client:
            api = f"/a/{page.public_id}/_api/notify"
            sent = await client.post(api, json={"event": "vote", "data": {}}, headers=OWN)
    assert sent.status_code == 200, sent.text
    assert {"event": "web.artifacts.event_refused", "watch_id": str(watch_id)}.items() <= next(
        log for log in logs if log["event"] == "web.artifacts.event_refused"
    ).items()
