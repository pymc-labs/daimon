"""Real-Postgres tests for the slack_turn_contexts store (leak-policy source)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.stores.slack_turn_contexts import (
    create_slack_turn_context,
    get_slack_turn_channels,
)
from sqlalchemy.ext.asyncio import AsyncSession

TENANT = uuid.uuid4()
ACCOUNT = uuid.uuid4()
NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


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
