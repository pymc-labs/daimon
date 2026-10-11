"""Which Discord process may be connected to the gateway.

During a deploy the old and the new Discord process overlap. Only the holder of
one PostgreSQL session advisory lock (one per database, so one per environment)
may open the gateway connection. The lock sits on the process's owner
connection (`hold_worker_owner`), so losing that connection loses the lease.

Postgres drops the lock the moment the holder's connection dies, but that
process may still be connected to Discord until it fails stop. The
`discord_gateway_lease` row therefore records the holder and whether it let go
cleanly; a successor that finds an unclean holder waits for `owner_is_alive`
to turn false before connecting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import structlog
from daimon.core.stores.worker_ownership import owner_is_alive
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

log = structlog.get_logger()

# Fixed, database-wide advisory key. Owner keys are random 63-bit values, so a
# collision is not a practical concern.
DISCORD_GATEWAY_LOCK_KEY = 0x44_49_53_43_47_41_54_45  # "DISCGATE"
GATEWAY_POLL_INTERVAL_S = 0.2
GATEWAY_OP_TIMEOUT_S = 5.0


class GatewayLeaseUnavailable(Exception):
    """The owner connection is gone; the process is failing stop."""


class GatewayLeaseLost(Exception):
    """The lease was taken over while this process still believed it held it."""


class GatewayLease:
    """The gateway lease of one process, taken on its owner connection.

    `connection` returns the owner connection currently in use (None while it is
    being replaced); `lock` serialises every use of it with the owner heartbeat.
    """

    held: bool

    def __init__(
        self,
        owner_key: int,
        *,
        connection: Callable[[], AsyncConnection | None],
        lock: asyncio.Lock,
        lock_key: int = DISCORD_GATEWAY_LOCK_KEY,
        poll_interval_s: float = GATEWAY_POLL_INTERVAL_S,
        op_timeout_s: float = GATEWAY_OP_TIMEOUT_S,
    ) -> None:
        self.owner_key = owner_key
        self.held = False
        self._connection = connection
        self._lock = lock
        self._lock_key = lock_key
        self._poll_interval_s = poll_interval_s
        self._op_timeout_s = op_timeout_s

    async def acquire(self, ownership_lost: asyncio.Event) -> int:
        """Block until this process may open the gateway; return milliseconds waited."""
        started = time.monotonic()
        announced = False
        taken_on: AsyncConnection | None = None
        previous: tuple[int, bool] | None = None  # (holder key, released cleanly)
        try:
            while True:
                if ownership_lost.is_set():
                    raise GatewayLeaseUnavailable("owner connection lost while waiting")
                try:
                    async with asyncio.timeout(self._op_timeout_s), self._lock:
                        conn = self._connection()
                        if conn is not taken_on:
                            taken_on = None  # the connection was replaced: the lock went with it
                        if conn is not None and taken_on is None:
                            got = await conn.scalar(
                                text("SELECT pg_try_advisory_lock(:key)"), {"key": self._lock_key}
                            )
                            if got:
                                taken_on = conn
                                row = (
                                    await conn.execute(
                                        text("""SELECT holder_owner_key, released_at IS NOT NULL
                                             FROM discord_gateway_lease WHERE id = 1""")
                                    )
                                ).first()
                                previous = (row[0], row[1]) if row is not None else None
                            await conn.commit()
                        if conn is not None and taken_on is not None:
                            # A process from before the lease may be connected
                            # without holding it: on the first rollout, and again
                            # after a rollback, when an old row is still there.
                            blocked = await self._other_owner_alive(conn)
                            if not blocked and previous is not None:
                                # Our own unreleased row means our earlier take
                                # committed but its acknowledgement was lost.
                                blocked = (
                                    not previous[1]
                                    and previous[0] != self.owner_key
                                    and await owner_is_alive(conn, previous[0])
                                )
                            if not blocked:
                                await conn.execute(
                                    text("""INSERT INTO discord_gateway_lease
                                         (id, holder_owner_key, acquired_at, released_at)
                                         VALUES (1, :key, clock_timestamp(), NULL)
                                         ON CONFLICT (id) DO UPDATE SET
                                           holder_owner_key = :key,
                                           acquired_at = clock_timestamp(),
                                           released_at = NULL"""),
                                    {"key": self.owner_key},
                                )
                                await conn.commit()
                                self.held = True
                                waited_ms = int((time.monotonic() - started) * 1000)
                                log.info("discord.gateway_lease_acquired", waited_ms=waited_ms)
                                return waited_ms
                            await conn.commit()
                except (Exception, TimeoutError) as err:
                    log.warning("discord.gateway_lease_retry", error_type=type(err).__name__)
                if not announced:
                    announced = True
                    log.info(
                        "discord.gateway_lease_waiting",
                        reason="holder_alive" if taken_on is not None else "lock_held",
                    )
                await asyncio.sleep(self._poll_interval_s)
        except asyncio.CancelledError:
            if taken_on is not None and not self.held:
                await self._unlock(taken_on)
            raise

    async def _other_owner_alive(self, conn: AsyncConnection) -> bool:
        """A live owner from before the lease. Lease-aware owners are ordered by the lock."""
        keys = (
            await conn.scalars(
                text("""SELECT owner_key FROM discord_worker_heartbeats
                     WHERE owner_key != :me AND NOT lease_aware"""),
                {"me": self.owner_key},
            )
        ).all()
        for key in keys:
            if await owner_is_alive(conn, key):
                return True
        return False

    async def confirm_after_reconnect(self, candidate: AsyncConnection) -> bool:
        """Retake the lock on a replacement owner connection; False if it moved on."""
        if not await candidate.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": self._lock_key}
        ):
            await candidate.commit()
            return False
        row = (
            await candidate.execute(
                text("""SELECT holder_owner_key, released_at IS NULL
                     FROM discord_gateway_lease WHERE id = 1""")
            )
        ).first()
        await candidate.commit()
        return row is not None and row[0] == self.owner_key and bool(row[1])

    async def release(self) -> None:
        """Hand the lease over after a clean close: record it, then unlock."""
        if not self.held:
            return
        try:
            async with asyncio.timeout(self._op_timeout_s), self._lock:
                conn = self._connection()
                if conn is None:
                    return
                await conn.execute(
                    text("""UPDATE discord_gateway_lease SET released_at = clock_timestamp()
                         WHERE id = 1 AND holder_owner_key = :key"""),
                    {"key": self.owner_key},
                )
                # Commit before unlocking: the unlock takes effect at once, and a
                # waiter that read the release uncommitted would treat it as
                # unclean and wait for this process to stop being alive.
                await conn.commit()
                await conn.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": self._lock_key})
                await conn.commit()
                self.held = False
                log.info("discord.gateway_lease_released")
        except (Exception, TimeoutError) as err:
            # The lock drops with the connection when the process exits.
            log.warning("discord.gateway_lease_release_failed", error_type=type(err).__name__)

    async def _unlock(self, conn: AsyncConnection) -> None:
        try:
            async with asyncio.timeout(self._op_timeout_s), self._lock:
                if self._connection() is conn:
                    await conn.scalar(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": self._lock_key}
                    )
                    await conn.commit()
        except (Exception, TimeoutError) as err:
            log.warning("discord.gateway_lease_abandon_failed", error_type=type(err).__name__)
