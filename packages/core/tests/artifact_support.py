"""A page-event watch on an artifact, for the artifact store and key tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from daimon.core.artifact_pages.keys import new_key_id, page_event_key
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.stores import watches
from daimon.core.stores.domain import WatchRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing.builders import new_thread_session
from daimon.testing.factories import make_tenant
from daimon.testing.watch_fixtures import install_poll_ports
from sqlalchemy.ext.asyncio import AsyncSession


async def watch_owner(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> ContinuationRequest:
    """A tenant whose owner has a live Discord thread, with watches switched on."""
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    await install_poll_ports(db)
    await watches.set_watch_control(db, scope="global", enabled=True)
    tenant = await make_tenant(db)
    principal = await get_or_create_platform_principal(
        db, tenant_id=tenant.id, platform="discord", external_id="123"
    )
    await new_thread_session(
        db,
        tenant_id=tenant.id,
        thread_id="4242",
        channel_id="4241",
        account_id=principal.account_id,
        ma_session_id="session-bound",
        ma_agent_id="agent-bound",
    )
    return ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="4241",
        thread_id="4242",
        requester_account_id=principal.account_id,
        requester_external_user_id="123",
        target_ma_agent_id="agent-bound",
        target_name="analyst",
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )


async def page_watch(
    db: AsyncSession,
    binding: ContinuationRequest,
    *,
    page_id: uuid.UUID,
    crypto_keys: tuple[str, ...],
    now: datetime,
) -> tuple[WatchRow, str]:
    """A watch on the page with a key derived as the web process derives it."""
    expires_at = now + timedelta(days=1)
    watch = await watches.register_page_watch(
        db,
        binding,
        instructions="Tally the votes.",
        page_id=page_id,
        expires_at=expires_at,
        now=now,
    )
    key_id = new_key_id(crypto_keys)
    key = page_event_key(crypto_keys, watch.id, key_id)
    assert key is not None
    await watches.rotate_signing_key(
        db, watch.id, key_id=key_id, key=key, expires_at=expires_at, now=now
    )
    return watch, key_id
