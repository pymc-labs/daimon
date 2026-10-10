"""Liveness of adapter processes holding a PostgreSQL session advisory lock."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def owner_is_alive(session: AsyncSession, owner_key: int) -> bool:
    """Read the owner lock without acquiring it or serializing unrelated turns."""
    return bool(
        await session.scalar(
            text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory' AND granted AND objsubid = 1
              AND database = (SELECT oid FROM pg_database
                              WHERE datname = current_database())
              AND classid = CAST(:upper AS oid) AND objid = CAST(:lower AS oid)
        )
    """),
            {"upper": owner_key >> 32, "lower": owner_key & 0xFFFFFFFF},
        )
    )
