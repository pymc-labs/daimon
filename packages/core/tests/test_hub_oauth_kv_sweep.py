"""The hub_oauth_kv sweep deletes only rows whose expires_at has passed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import structlog
from daimon.core.hub_oauth_kv_sweep import sweep_expired_hub_oauth_kv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


async def _insert(session: AsyncSession, *, key: str, expires_at: datetime | None) -> None:
    await session.execute(
        text(
            "INSERT INTO hub_oauth_kv (collection, key, value, ttl, created_at, expires_at) "
            "VALUES ('discord__mcp-oauth-transactions', :key, '{}'::jsonb, NULL, now(), :exp)"
        ),
        {"key": key, "exp": expires_at},
    )


async def test_sweep_deletes_expired_rows_and_keeps_live_and_permanent_rows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    await _insert(db_session, key="expired", expires_at=now - timedelta(seconds=1))
    await _insert(db_session, key="live", expires_at=now + timedelta(hours=1))
    await _insert(db_session, key="permanent", expires_at=None)
    await db_session.commit()

    deleted = await sweep_expired_hub_oauth_kv(db_session_factory, now=now)

    assert deleted == 1, f"exactly the expired row should be deleted, got {deleted}"
    remaining = (
        (await db_session.execute(text("SELECT key FROM hub_oauth_kv ORDER BY key")))
        .scalars()
        .all()
    )
    assert list(remaining) == ["live", "permanent"], f"unexpected survivors: {remaining!r}"


async def test_sweep_warns_when_it_fills_its_batch(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    await _insert(db_session, key="expired-1", expires_at=now - timedelta(seconds=1))
    await _insert(db_session, key="expired-2", expires_at=now - timedelta(seconds=1))
    await db_session.commit()

    with structlog.testing.capture_logs() as logs:
        deleted = await sweep_expired_hub_oauth_kv(db_session_factory, now=now, limit=1)

    assert deleted == 1, f"the batch limit must cap the delete, got {deleted}"
    backlog = [e for e in logs if e["event"] == "hub_oauth_kv_sweep.backlog"]
    assert backlog and backlog[0]["log_level"] == "warning", (
        f"a full batch must be reported at warning level, got {logs!r}"
    )
