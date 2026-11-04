"""The per-source lock that keeps two syncs of one repo branch apart.

The push queue holds it for each job, and a manual resync from the web panel
holds it for each source it syncs, so neither runs over the other in any process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def resync_lock_key(repo_full_name: str, ref: str) -> str:
    """The advisory lock name for one repo and ref; GitHub names ignore case."""
    return f"github-push-resync:{repo_full_name.casefold()}:{ref}"


@asynccontextmanager
async def hold_resync_locks(
    session_factory: async_sessionmaker[AsyncSession], repo_refs: Collection[tuple[str, str]]
) -> AsyncIterator[bool]:
    """Try each (repo, ref) lock for the block; yield whether all of them were free.

    Transaction-scoped, so every lock taken is released when the block ends,
    whether it ran or raised.
    """
    async with session_factory() as session, session.begin():
        free = True
        for key in sorted({resync_lock_key(repo, ref) for repo, ref in repo_refs}):
            taken = await session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )
            if not taken.scalar_one():
                free = False
                break
        yield free
