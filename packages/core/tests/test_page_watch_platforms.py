"""Page watches beyond Discord: web owners run as their linked principal, permanent and
new-thread page watches and the `select` filter."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import TaskContinuation
from daimon.core.artifact_pages.keys import new_key_id, page_event_key
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.page_events import sign_page_event
from daimon.core.continuity.poll_tools import validate_poll
from daimon.core.stores import watches
from daimon.core.stores.domain import WatchRow
from daimon.core.stores.identity import create_platform_principal, get_or_create_platform_principal
from daimon.core.web_identity import place_of, thread_key
from daimon.testing.builders import new_thread_session
from daimon.testing.factories import make_tenant
from daimon.testing.watch_fixtures import install_poll_ports
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.fresh_schema


async def _web_owner(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> ContinuationRequest:
    """A web person linked to a Discord account, chatting in a live web thread."""
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    await install_poll_ports(db)
    await watches.set_watch_control(db, scope="global", enabled=True)
    tenant = await make_tenant(db)
    linked = await get_or_create_platform_principal(
        db, tenant_id=tenant.id, platform="discord", external_id="d-owner"
    )
    person, thread = uuid.uuid4(), uuid.uuid4()
    await create_platform_principal(
        db,
        tenant_id=tenant.id,
        platform="web",
        external_id=str(person),
        account_id=linked.account_id,
    )
    await new_thread_session(
        db,
        tenant_id=tenant.id,
        platform="web",
        thread_id=thread_key(thread),
        channel_id=place_of(person),
        account_id=linked.account_id,
        ma_session_id="session-web",
        ma_agent_id="agent-web",
    )
    return ContinuationRequest(
        tenant_id=tenant.id,
        platform="web",
        principal_platform="discord",
        parent_channel_id=place_of(person),
        thread_id=thread_key(thread),
        requester_account_id=linked.account_id,
        requester_external_user_id="d-owner",
        target_ma_agent_id="agent-web",
        target_name="analyst",
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )


async def _send(
    db: AsyncSession, watch: WatchRow, page_id: uuid.UUID, body: object
) -> uuid.UUID | None:
    keys, now = (Fernet.generate_key().decode(),), datetime.now(UTC)
    key_id = new_key_id(keys)
    key = page_event_key(keys, watch.id, key_id)
    assert key is not None
    await watches.rotate_signing_key(
        db, watch.id, key_id=key_id, key=key, expires_at=now + timedelta(days=1), now=now
    )
    signed = sign_page_event(
        key=key,
        watch_id=watch.id,
        page_id=page_id,
        key_id=key_id,
        body=json.dumps(body).encode(),
        now=now,
    )
    return await watches.accept_page_event(db, signed, key=key)


async def test_a_permanent_web_page_watch_reports_in_a_new_thread_as_its_linked_owner(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = await _web_owner(db_session, monkeypatch)
    page_id = uuid.uuid4()
    watch = await watches.register_page_watch(
        db_session,
        binding,
        instructions="Tally the votes.",
        page_id=page_id,
        expires_at=None,
        now=datetime.now(UTC),
        delivery="new_thread",
        select_path=".data.vote",
    )
    assert (watch.platform, watch.principal_platform) == ("web", "discord")
    assert (watch.expires_at, watch.delivery, watch.session_id) == (None, "new_thread", None)
    assert await _send(db_session, watch, page_id, {"data": {}}) is None, "selected nothing"
    wake = await _send(db_session, watch, page_id, {"data": {"vote": "yes"}})
    row = await db_session.scalar(
        select(TaskContinuation).where(TaskContinuation.idempotency_key == wake)
    )
    assert row is not None
    assert (row.platform, row.reason, row.requester_external_user_id) == (
        "web",
        "watch_report",
        "d-owner",
    )


async def test_a_web_watch_polls_a_pages_data(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = await _web_owner(db_session, monkeypatch)
    query = {"slug": "votes", "collection": "_events", "order_by": "at", "limit": 50}
    watch = await watches.register_poll_watch(
        db_session,
        binding,
        instructions="Say when someone votes.",
        tool=watches.WEB_POLL_TOOL,
        arguments=query,
        projection=".rows",
        rule="added",
        key=".rows[].id",
        interval=300,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        now=datetime.now(UTC),
    )
    assert (watch.platform, watch.source, watch.source_state["tool"]) == (
        "web",
        "poll",
        "artifact_db_query",
    )
    with pytest.raises(ValueError, match="1 to 100"):
        validate_poll(
            watches.WEB_POLL_TOOL,
            {**query, "limit": 500},
            projection=".rows",
            rule="change",
            interval=300,
        )
