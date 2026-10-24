"""Notice claims skip archived workspaces and historical expiry rows."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from daimon.core.continuity.watch_imports import ImportedWatch
from daimon.core.continuity.watch_notices import poll_watch_notices
from daimon.core.stores import watches
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_watch_imports import definition as definition

pytestmark = pytest.mark.fresh_schema


async def test_archived_tenant_notice_does_not_stop_batch(
    db_session_factory: async_sessionmaker[AsyncSession], definition: ImportedWatch
) -> None:
    from sqlalchemy import text

    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        archived = await watches.import_watch(db, definition, now=now)
        await db.execute(
            text("UPDATE tenants SET archived_at=:now WHERE id=:id"),
            {"now": now, "id": archived.tenant_id},
        )
        live = await make_tenant(db)
        principal = await get_or_create_platform_principal(
            db, tenant_id=live.id, platform="discord", external_id="456"
        )
        await create_thread_session(
            db,
            tenant_id=live.id,
            platform="discord",
            thread_id="5252",
            channel_id="5251",
            account_id=principal.account_id,
            ma_session_id="session-live",
            ma_agent_id="agent-live",
        )
        binding = definition.os_binding.model_copy(
            update={
                "tenant_id": live.id,
                "requester_account_id": principal.account_id,
                "requester_external_user_id": "456",
                "parent_channel_id": "5251",
                "thread_id": "5252",
                "target_ma_agent_id": "agent-live",
            }
        )
        expected = await watches.import_watch(
            db, definition.model_copy(update={"os_binding": binding}), now=now
        )
        # Give the archived row priority under the claim's creation ordering.
        await db.execute(
            text("UPDATE watches SET created_at=:created WHERE id=:id"),
            {"created": now - timedelta(days=1), "id": archived.id},
        )
    deliver = AsyncMock(return_value=True)
    await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()
    assert deliver.await_args is not None
    assert deliver.await_args.args[0].id == expected.id
    async with db_session_factory() as db:
        unchanged = await watches.get_watch(db, archived.id)
        assert unchanged is not None
        assert "notice_lease" not in unchanged.source_state


@pytest.mark.parametrize("pending_import", [False, True])
@pytest.mark.parametrize(
    "age,expected",
    [
        (timedelta(minutes=5), True),
        (timedelta(hours=24), True),
        (timedelta(hours=24, microseconds=1), False),
        (timedelta(days=30), False),
    ],
)
async def test_expired_notices_are_bounded_by_expiry_age(
    db_session_factory: async_sessionmaker[AsyncSession],
    definition: ImportedWatch,
    age: timedelta,
    expected: bool,
    pending_import: bool,
) -> None:
    from sqlalchemy import text

    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        row = await watches.import_watch(db, definition, now=now)
        await db.execute(
            text("UPDATE watches SET status='expired', expires_at=:expiry WHERE id=:id"),
            {"expiry": now - age, "id": row.id},
        )
        if not pending_import:
            await db.execute(
                text("UPDATE watches SET source_state='{}' WHERE id=:id"), {"id": row.id}
            )
        notice = await watches.claim_import_notice(db, now=now)
        assert (notice is not None) is expected
        if notice is not None:
            assert notice[0].id == row.id
            await watches.settle_import_notice(db, notice[0], delivered=True)
            assert await watches.claim_import_notice(db, now=now) is None
        current = await watches.get_watch(db, row.id)
        assert current is not None
        assert current.status == "expired"
