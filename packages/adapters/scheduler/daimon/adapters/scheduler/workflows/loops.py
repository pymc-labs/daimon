"""The workflow engine's loops: starts, steps, waits, heartbeat, sweep and schedules.

Each loop repeats one `scheduler_step` on its interval; NOTIFY on `workflow_start` and
`workflow_ready` wakes the starts and steps loops early. Steps run as tasks of this
process under a lease the heartbeat renews; a lease lost to a cancel or a reclaim
cancels its task, and a task whose process died is reclaimed once its lease runs out.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Final

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import structlog
from daimon.adapters.scheduler.boundary import scheduler_step
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.stores import tenants, workflow_runs
from daimon.core.workflows import runs
from daimon.core.workflows.engine import RunEnd
from daimon.core.workflows.features import KEYS, workflow_features
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

LEASE: Final = timedelta(minutes=5)
INTERVALS_S: Final = {
    "starts": 2.0,
    "steps": 2.0,
    "waits": 30.0,
    "heartbeat": 60.0,
    "sweep": 86400.0,
    "schedules": 30.0,
}
_BATCH: Final = 50
_LISTEN_RETRY_S: Final = 5.0


class WorkflowEngine:
    """This process's share of the engine: its lease owner name and running step tasks."""

    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        settings: Settings,
        executors: Mapping[str, runs.StepExecutor],
        *,
        owner: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sm = sm
        self.settings = settings
        self.executors = executors
        self.owner = owner or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.clock = clock
        self.tasks: dict[uuid.UUID, tuple[asyncio.Task[None], int]] = {}
        self.wake = {"starts": asyncio.Event(), "steps": asyncio.Event()}

    async def drain(self) -> None:
        """Wait for the running step tasks (the `--once` path and tests)."""
        while self.tasks:
            await asyncio.wait([task for task, _ in self.tasks.values()])

    def spawn(self, claimed: runs.Claimed) -> None:
        step_id = claimed.step.id
        task = asyncio.create_task(
            runs.execute(self.sm, self.settings, self.executors, claimed, clock=self.clock)
        )
        self.tasks[step_id] = (task, claimed.step.generation)

        def done(finished: asyncio.Task[None]) -> None:
            self.tasks.pop(step_id, None)
            if not finished.cancelled() and (error := finished.exception()) is not None:
                log.error("workflows.step.crashed", step_id=str(step_id), error=repr(error))

        task.add_done_callback(done)


@scheduler_step
async def starts_step(engine: WorkflowEngine) -> None:
    """Accepted trigger events become runs; queued runs begin when their workflow has room."""
    now = engine.clock()
    await runs.start_from_trigger_events(engine.sm, engine.settings, now=now)
    async with engine.sm() as session:
        queued = await workflow_runs.queued_runs(session, limit=_BATCH)
    for run_id in queued:
        await runs.promote_queued(engine.sm, engine.settings, run_id, now=now)


@scheduler_step
async def steps_step(engine: WorkflowEngine) -> None:
    """Claim due ready steps within the deployment, workspace and run-as caps."""
    caps = engine.settings.workflows
    now = engine.clock()
    async with engine.sm() as session:
        total, per_tenant, per_owner = await workflow_runs.running_counts(session)
        free = caps.max_concurrent_steps - total
        due = await workflow_runs.ready_steps(session, now=now, limit=max(free, 0) * 3)
    for step_id, tenant_id, owner in due:
        if free <= 0:
            return
        if per_tenant.get(tenant_id, 0) >= caps.per_tenant_agent_steps:
            continue
        if owner is not None and per_owner.get(owner, 0) >= caps.per_owner_agent_steps:
            continue
        claimed = await runs.claim(
            engine.sm, step_id=step_id, owner=engine.owner, lease=LEASE, now=now
        )
        if claimed is None:
            continue
        free -= 1
        per_tenant[tenant_id] = per_tenant.get(tenant_id, 0) + 1
        if owner is not None:
            per_owner[owner] = per_owner.get(owner, 0) + 1
        engine.spawn(claimed)


@scheduler_step
async def waits_step(engine: WorkflowEngine) -> None:
    """Recheck blocked steps and time out runs past their deadline."""
    now = engine.clock()
    async with engine.sm() as session:
        blocked = await workflow_runs.due_waits(session, kind="blocked", now=now, limit=_BATCH)
        overdue = await workflow_runs.overdue_runs(session, now=now, limit=_BATCH)
    for wait in blocked:
        await runs.recheck_blocked(engine.sm, engine.settings, wait, now=now)
    end = RunEnd("timed_out", reason="deadline", message="The run passed its deadline")
    for run_id in overdue:
        await runs.stop_run(engine.sm, engine.settings, run_id=run_id, end=end, now=now)


@scheduler_step
async def heartbeat_step(engine: WorkflowEngine) -> None:
    """Renew held leases, cancel tasks that lost theirs, end runs over budget, reclaim the dead."""
    now = engine.clock()
    held = {step_id: generation for step_id, (_, generation) in engine.tasks.items()}
    async with engine.sm.begin() as session:
        kept = await workflow_runs.renew_leases(
            session, owner=engine.owner, held=held, until=now + LEASE
        )
    for step_id in set(held) - kept:
        if (entry := engine.tasks.get(step_id)) is not None:
            entry[0].cancel()
    async with engine.sm() as session:
        over = await workflow_runs.over_budget_runs(session, limit=_BATCH)
        expired = await workflow_runs.expired_leases(session, now=now, limit=_BATCH)
    end = RunEnd("failed", reason="over_budget", message="The run spent its budget")
    for run_id in over:
        await runs.stop_run(engine.sm, engine.settings, run_id=run_id, end=end, now=now)
    for step_id in expired:
        if step_id not in engine.tasks:
            await runs.reclaim(engine.sm, engine.settings, step_id=step_id, now=now)


@scheduler_step
async def sweep_step(engine: WorkflowEngine) -> None:
    """Clear old payloads and delete finished runs past their workspace's retention."""

    async def retention(tenant_id: uuid.UUID) -> int:
        async with engine.sm() as session:
            tenant = await tenants.get_tenant(session, tenant_id)
        platform = tenant.platform if tenant else "web"
        place = FeaturePlace(tenant_id, platform)
        snapshot = await resolve_features(engine.sm, engine.settings, place, keys=KEYS)
        return workflow_features(snapshot).retention_days

    await runs.sweep(engine.sm, engine.settings, now=engine.clock(), retention_days=retention)


@scheduler_step
async def schedules_step(engine: WorkflowEngine) -> None:
    """Schedule triggers fire here (M6); nothing yet."""
    del engine


STEPS: Final[dict[str, Callable[[WorkflowEngine], Awaitable[None]]]] = {
    "starts": starts_step,
    "steps": steps_step,
    "waits": waits_step,
    "heartbeat": heartbeat_step,
    "sweep": sweep_step,
    "schedules": schedules_step,
}


async def run_once(engine: WorkflowEngine) -> None:
    """One pass of every loop, then wait for the steps it started (`--once`)."""
    for step in STEPS.values():
        await step(engine)
    await engine.drain()


async def run_loops(
    engine: WorkflowEngine, *, stop_event: asyncio.Event, db: AsyncEngine | None = None
) -> None:
    """Every loop until `stop_event`; `db` listens for wakeups (polling alone without it).

    Running step tasks are cancelled on stop; their leases run out and the next engine
    reclaims them.
    """

    async def repeat(name: str, step: Callable[[WorkflowEngine], Awaitable[None]]) -> None:
        wake = engine.wake.get(name)
        while not stop_event.is_set():
            await step(engine)
            waiters = [asyncio.ensure_future(stop_event.wait())]
            if wake is not None:
                wake.clear()
                waiters.append(asyncio.ensure_future(wake.wait()))
            _, pending = await asyncio.wait(
                waiters, timeout=INTERVALS_S[name], return_when=asyncio.FIRST_COMPLETED
            )
            for waiter in pending:
                waiter.cancel()

    async with asyncio.TaskGroup() as loops:
        listener = loops.create_task(_listen(engine, db)) if db is not None else None
        for name, step in STEPS.items():
            loops.create_task(repeat(name, step))
        await stop_event.wait()
        if listener is not None:
            listener.cancel()
    for task, _ in list(engine.tasks.values()):
        task.cancel()


async def _listen(engine: WorkflowEngine, db: AsyncEngine) -> None:
    channels = {runs.START_CHANNEL: engine.wake["starts"], runs.READY_CHANNEL: engine.wake["steps"]}
    while True:
        try:
            await _listen_once(db, channels)
        except (OSError, SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError) as err:
            log.warning("workflows.listen.lost", err_type=type(err).__name__)
        await asyncio.sleep(_LISTEN_RETRY_S)


async def _listen_once(db: AsyncEngine, channels: Mapping[str, asyncio.Event]) -> None:
    def on_notify(_conn: object, _pid: int, channel: str, _payload: str) -> None:
        if (event := channels.get(channel)) is not None:
            event.set()

    async with db.connect() as conn:
        raw = (await conn.get_raw_connection()).driver_connection
        if raw is None:
            return
        lost = asyncio.Event()

        def on_lost(_conn: object) -> None:
            lost.set()

        raw.add_termination_listener(on_lost)
        for channel in channels:
            await raw.add_listener(channel, on_notify)
        try:
            await lost.wait()
        finally:
            if not raw.is_closed():
                for channel in channels:
                    with contextlib.suppress(asyncpg.PostgresError):
                        await raw.remove_listener(channel, on_notify)
