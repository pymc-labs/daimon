"""The sync worker's loop: one leader decides which sources are due and runs them.

Leadership is a session advisory lock on a connection of its own, so two workers never both
schedule; the lock goes with the connection. Runs are single per source by their own lock
(`engine.run_source`), so a leader that lost the lock for a moment cannot double a run.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import psycopg
import structlog
from daimon.core.config import Settings
from daimon.data import acl, pool
from daimon.data.dsn import libpq_dsn
from daimon.data.locks import advisory_key
from daimon.data.schema import Schemas
from daimon.data.sync.embedding import embed_pending
from daimon.data.sync.engine import STALE_RUN, Env, run_source, try_lock, unlock
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

log = structlog.get_logger()

TICK: Final = 15.0
"""Seconds between looks at what is due."""
RUN_BUDGET: Final = 600.0
"""Seconds one run works before it yields as `partial` and the next run continues."""
DEFAULT_MINUTES: Final = 60
"""Minutes between runs, unless the source's config sets `sync_minutes`."""
BACKOFF_MINUTES: Final = (5, 360)
"""A failing source waits 5, 10, 20... minutes between runs, at most 360."""
DAY: Final = timedelta(days=1)
KEEP_RUNS: Final = timedelta(days=30)
FIRST_MAINTENANCE: Final = timedelta(minutes=10)

_CANDIDATES: Final = """
select s.id, s.config, c.requested_at, c.last_status, c.last_sync_at,
       coalesce(c.errors, 0) as errors,
       (select max(r.finished_at) from {d}.sync_runs r where r.source_id = s.id) as last_run
from {d}.sources s left join {d}.source_cursors c on c.source_id = s.id
where s.status in ('active', 'error') and s.publication <> 'retired'"""


@dataclass(frozen=True)
class Due:
    id: UUID
    trigger: str
    rank: int


def _due(row: DictRow, now: datetime) -> Due | None:
    """A source's turn: asked for, never run, left partial, past its backoff, or past its
    interval. Lower rank first."""
    if row["requested_at"] is not None:
        return Due(row["id"], "manual", 0)
    status: str | None = row["last_status"]
    if status is None:
        return Due(row["id"], "schedule", 1)
    if status == "partial":
        return Due(row["id"], "schedule", 2)
    if status == "error":
        first, cap = BACKOFF_MINUTES
        wait = min(first * 2 ** max(int(row["errors"]) - 1, 0), cap)
        last = row["last_run"] or row["last_sync_at"]
        return (
            Due(row["id"], "schedule", 3)
            if last is None or now >= last + timedelta(minutes=wait)
            else None
        )
    config: dict[str, object] = row["config"] or {}
    minutes = int(str(config.get("sync_minutes") or DEFAULT_MINUTES))
    last = row["last_sync_at"]
    due = last is None or now >= last + timedelta(minutes=minutes)
    return Due(row["id"], "schedule", 4) if due else None


async def due(
    conn: AsyncConnection[DictRow], schemas: Schemas, now: datetime | None = None
) -> list[Due]:
    """The sources to run now, most urgent first."""
    now = now or datetime.now(UTC)
    cur = await conn.execute(_CANDIDATES.format(d=schemas.data).encode())
    found = [d for r in await cur.fetchall() if (d := _due(r, now))]
    return sorted(found, key=lambda d: d.rank)


async def recover(conn: AsyncConnection[DictRow], schemas: Schemas) -> int:
    """Close runs a restart cut off: still `running` after `STALE_RUN` with no one holding the
    source's lock. The source resumes from its cursor on the next tick."""
    d = schemas.data
    cur = await conn.execute(
        f"select id, source_id from {d}.sync_runs where status = 'running'"
        " and started_at < now() - %s".encode(),
        (STALE_RUN,),
    )
    n = 0
    for r in await cur.fetchall():
        key = advisory_key(d, f"source:{r['source_id']}")
        if not await try_lock(conn, key):
            continue  # a live run holds it
        try:
            async with conn.transaction():
                await conn.execute(
                    f"update {d}.sync_runs set status = 'error', finished_at = now(),"
                    " error = 'interrupted' where id = %s and status = 'running'".encode(),
                    (r["id"],),
                )
                await conn.execute(
                    f"update {d}.source_cursors set last_status = 'partial'"
                    " where source_id = %s".encode(),
                    (r["source_id"],),
                )
            n += 1
        finally:
            await unlock(conn, key)
    return n


async def take_lead(url: str, schemas: Schemas) -> AsyncConnection[DictRow] | None:
    """The leader's own connection if the lock was free, else None."""
    conn = await AsyncConnection[DictRow].connect(
        libpq_dsn(url), autocommit=True, row_factory=dict_row
    )
    try:
        if await try_lock(conn, advisory_key(schemas.data, "scheduler")):
            return conn
    except BaseException:
        await conn.close()
        raise
    await conn.close()
    return None


class Maintenance:
    """Daily jobs, in memory: the first runs shortly after start, then each day."""

    def __init__(self, start: datetime) -> None:
        self.last = start - DAY + FIRST_MAINTENANCE

    def due(self, now: datetime) -> bool:
        return now - self.last >= DAY

    async def run(self, conn: AsyncConnection[DictRow], env: Env, now: datetime) -> None:
        self.last = now
        d = env.schemas.data
        await conn.execute(
            f"delete from {d}.sync_runs"
            " where started_at < now() - %s and status <> 'running'".encode(),
            (KEEP_RUNS,),
        )
        async with conn.transaction():
            wrong = {k: n for k, n in (await acl.nightly(conn, env.schemas)).items() if n}
        if wrong:
            log.warning("data.acl_repaired", counts=wrong)
        if env.embed:
            await embed_pending(conn, env.schemas)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def _work(settings: Settings, env: Env, job: Due, inflight: set[UUID]) -> None:
    try:
        async with pool.connection(settings) as conn:
            result = await run_source(conn, env, job.id, job.trigger, RUN_BUDGET)
        log.info(
            "sync.ran",
            source=str(job.id),
            status=result.status,
            added=result.added,
            updated=result.updated,
            deleted=result.deleted,
        )
    except Exception:
        log.exception("sync.crashed", source=str(job.id))
    finally:
        inflight.discard(job.id)


async def _maintain(settings: Settings, env: Env, m: Maintenance, flag: list[bool]) -> None:
    try:
        async with pool.connection(settings) as conn:
            await m.run(conn, env, datetime.now(UTC))
    except Exception:
        log.exception("data.maintenance_failed")
    finally:
        flag[0] = False


async def lead(
    settings: Settings, env: Env, leader: AsyncConnection[DictRow], stop: asyncio.Event
) -> None:
    """Schedule until stopped or the lock's connection is lost."""
    inflight: set[UUID] = set()
    tasks: set[asyncio.Task[None]] = set()
    maintenance, maintaining = Maintenance(datetime.now(UTC)), [False]
    limit = settings.data.worker_concurrency
    try:
        async with pool.connection(settings) as conn:
            if n := await recover(conn, env.schemas):
                log.info("sync.recovered_runs", count=n)
        while not stop.is_set():
            try:
                await leader.execute(b"select 1")  # lost: the lock went with the connection
            except psycopg.Error:
                log.warning("sync.leader_lost")
                return
            async with pool.connection(settings) as conn:
                jobs = await due(conn, env.schemas)
            for job in jobs:
                if len(inflight) >= limit:
                    break
                if job.id not in inflight:
                    inflight.add(job.id)
                    task = asyncio.create_task(_work(settings, env, job, inflight))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
            if not maintaining[0] and maintenance.due(datetime.now(UTC)):
                maintaining[0] = True
                task = asyncio.create_task(_maintain(settings, env, maintenance, maintaining))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            await _wait(stop, TICK)
    finally:
        for task in tasks:
            task.cancel()  # a cut run is closed by `recover` on the next start
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    """Take the lead whenever it is free and schedule until `stop` is set."""
    env = Env.of(settings)
    while not stop.is_set():
        leader = await take_lead(str(settings.database.url), env.schemas)
        if leader is None:
            await _wait(stop, TICK)
            continue
        log.info("sync.leader")
        try:
            await lead(settings, env, leader, stop)
        finally:
            await leader.close()
        await _wait(stop, 1.0)
