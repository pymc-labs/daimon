"""Real-Postgres tests for the slack_turn_contexts store (leak-policy source)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.stores.slack_turn_contexts import (
    create_slack_turn_context,
    delete_slack_turn_context,
    get_slack_turn_channels,
)
from sqlalchemy.ext.asyncio import AsyncSession

TENANT = uuid.uuid4()
ACCOUNT = uuid.uuid4()
NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


async def test_create_then_get_returns_channel(db_session: AsyncSession) -> None:
    await create_slack_turn_context(
        db_session,
        tenant_id=TENANT,
        account_id=ACCOUNT,
        channel_id="C1",
        thread_ts="1.1",
        started_at=NOW,
    )
    channels = await get_slack_turn_channels(
        db_session, tenant_id=TENANT, account_id=ACCOUNT, cutoff=NOW - timedelta(minutes=60)
    )
    assert channels == frozenset({"C1"}), "live row must surface its channel"


async def test_get_excludes_rows_older_than_cutoff(db_session: AsyncSession) -> None:
    await create_slack_turn_context(
        db_session,
        tenant_id=TENANT,
        account_id=ACCOUNT,
        channel_id="C-stale",
        thread_ts="1.1",
        started_at=NOW - timedelta(minutes=61),
    )
    channels = await get_slack_turn_channels(
        db_session, tenant_id=TENANT, account_id=ACCOUNT, cutoff=NOW - timedelta(minutes=60)
    )
    assert channels == frozenset(), "stale rows (crashed process) must be ignored"


async def test_get_is_scoped_to_tenant_and_account(db_session: AsyncSession) -> None:
    await create_slack_turn_context(
        db_session,
        tenant_id=TENANT,
        account_id=ACCOUNT,
        channel_id="C1",
        thread_ts="1.1",
        started_at=NOW,
    )
    other = await get_slack_turn_channels(
        db_session, tenant_id=TENANT, account_id=uuid.uuid4(), cutoff=NOW - timedelta(minutes=60)
    )
    assert other == frozenset(), "another account must not see this account's turn context"


async def test_delete_removes_row(db_session: AsyncSession) -> None:
    row = await create_slack_turn_context(
        db_session,
        tenant_id=TENANT,
        account_id=ACCOUNT,
        channel_id="C1",
        thread_ts="1.1",
        started_at=NOW,
    )
    assert await delete_slack_turn_context(db_session, id=row.id) == 1
    channels = await get_slack_turn_channels(
        db_session, tenant_id=TENANT, account_id=ACCOUNT, cutoff=NOW - timedelta(minutes=60)
    )
    assert channels == frozenset(), "deleted turn context must disappear"


async def test_two_live_channels_both_returned(db_session: AsyncSession) -> None:
    for i, ch in enumerate(("C1", "C2")):
        await create_slack_turn_context(
            db_session,
            tenant_id=TENANT,
            account_id=ACCOUNT,
            channel_id=ch,
            thread_ts=f"{i}.1",
            started_at=NOW,
        )
    channels = await get_slack_turn_channels(
        db_session, tenant_id=TENANT, account_id=ACCOUNT, cutoff=NOW - timedelta(minutes=60)
    )
    assert channels == frozenset({"C1", "C2"}), (
        "concurrent turns in two channels must both be visible (caller fails closed)"
    )
