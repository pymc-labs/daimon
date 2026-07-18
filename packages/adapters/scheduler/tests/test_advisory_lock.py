"""Advisory-lock contention tests for the scheduler entrypoint.

Two-process behavior:
- ``_acquire_advisory_lock`` returns a held connection iff
  ``pg_try_advisory_lock`` succeeds; returns ``None`` otherwise.
- The lock is connection-scoped: once the holding connection closes (or
  unlocks), a subsequent acquire on the same key succeeds.

These tests use the real test Postgres — schema-per-test isolation does
NOT cover advisory locks (locks are session-scoped at the Postgres
``pg_locks`` level, independent of search_path), so we use a unique
per-test key.
"""

from __future__ import annotations

import os
import uuid

import pytest
from daimon.adapters.scheduler.main import _acquire_advisory_lock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _test_dsn() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for advisory-lock tests")
    return url


def _unique_key() -> int:
    """Generate a per-test int64 key from a uuid. Hashing avoids collision
    with any other test or process holding a different scheduler key."""
    return uuid.uuid4().int >> 65  # fits in signed int64, distinct per test


async def test_acquire_returns_connection_when_uncontended() -> None:
    engine = create_async_engine(_test_dsn())
    try:
        key = _unique_key()
        conn = await _acquire_advisory_lock(engine, key)
        assert conn is not None, "uncontended acquire must return a held connection"
        try:
            # Verify the lock is actually held by this connection.
            result = await conn.execute(
                text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=:k"),
                {"k": key & 0xFFFFFFFF},
            )
            count = result.scalar_one()
            assert count >= 1, "pg_locks should record an advisory lock for this key"
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            await conn.close()
    finally:
        await engine.dispose()


async def test_second_acquire_returns_none_when_first_holds_lock() -> None:
    engine = create_async_engine(_test_dsn())
    try:
        key = _unique_key()
        first = await _acquire_advisory_lock(engine, key)
        assert first is not None, "first acquire should succeed"
        try:
            second = await _acquire_advisory_lock(engine, key)
            assert second is None, "second acquire on the same key must return None"
        finally:
            await first.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            await first.close()

        # After release, a fresh acquire succeeds.
        third = await _acquire_advisory_lock(engine, key)
        assert third is not None, "after unlock, acquire must succeed"
        await third.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        await third.close()
    finally:
        await engine.dispose()
