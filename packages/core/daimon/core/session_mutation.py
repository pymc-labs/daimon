"""Serialize session sends with retirement, without holding row locks over MA I/O.

This advisory lock is acquired after preparation's advisory lock, when present.
Sends take no policy, account or binding row locks. Retirement publishes its
closure under the tenant lock after MA I/O. Handoff and policy writers never
acquire this fence. Replacement publishes the successor before archiving.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from daimon.core.errors import SessionRetired as SessionRetired
from daimon.core.stores.thread_sessions import require_writable_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@asynccontextmanager
async def session_mutation_fence(
    factory: async_sessionmaker[AsyncSession], session_id: str, *, check: bool = True
) -> AsyncIterator[None]:
    async with factory.begin() as db:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"session_mutation:{session_id}"},
        )
        if check:
            await require_writable_session(db, session_id)
        yield
