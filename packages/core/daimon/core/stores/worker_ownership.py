"""Liveness of adapter processes holding a PostgreSQL session advisory lock."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

# From the last committed heartbeat, stop occurs within probe interval + probe
# timeout + reconnect budget + hard exit. The margin covers event-loop scheduling
# and connection teardown; this timing bound is required for exclusion.
OWNER_PROBE_INTERVAL_S = 5
OWNER_PROBE_TIMEOUT_S = 5
OWNER_RETRY_BUDGET_S = 30
OWNER_HARD_EXIT_S = 100
OWNER_SCHEDULING_MARGIN_S = 40
OWNER_LOSS_GRACE_S = (
    OWNER_PROBE_INTERVAL_S
    + OWNER_PROBE_TIMEOUT_S
    + OWNER_RETRY_BUDGET_S
    + OWNER_HARD_EXIT_S
    + OWNER_SCHEDULING_MARGIN_S
)


async def owner_is_alive(session: AsyncSession | AsyncConnection, owner_key: int) -> bool:
    """Keep a disconnected owner protected until its fail-stop bound expires."""
    return bool(
        await session.scalar(
            text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory' AND granted AND objsubid = 1
              AND database = (SELECT oid FROM pg_database
                              WHERE datname = current_database())
              AND classid = CAST(:upper AS oid) AND objid = CAST(:lower AS oid)
        ) OR EXISTS (
            SELECT 1 FROM discord_worker_heartbeats
            WHERE owner_key = :key
              AND seen_at > clock_timestamp() - (:grace * interval '1 second')
        )
    """),
            {
                "upper": owner_key >> 32,
                "lower": owner_key & 0xFFFFFFFF,
                "key": owner_key,
                "grace": OWNER_LOSS_GRACE_S,
            },
        )
    )
