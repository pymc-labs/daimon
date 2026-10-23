"""Durable isolated workers. A scheduler lease owns execution, never a chat turn."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import structlog
from daimon.core.billing import is_over_cap
from daimon.core.config import LongJobsSettings
from daimon.core.errors import StoreError
from daimon.core.long_job_models import LongJob, LongJobManifest, LongJobState
from daimon.core.stores import long_jobs as store
from daimon.core.stores.tenants import list_tenants_by_platform
from daimon.core.tenant_balance import is_over_balance
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class WorkerVanished(Exception):
    """The worker session was deleted, archived or terminated."""


class WorkerBudgetReached(Exception):
    """The provider stopped before its next model request exceeded the job budget."""


class WorkerPort(Protocol):
    async def create_or_find(self, job: LongJob) -> str: ...
    async def has_started(self, job: LongJob) -> bool: ...
    async def drive(self, job: LongJob, *, resume: bool) -> None: ...
    async def manifest(self, job: LongJob) -> LongJobManifest | None: ...
    async def interrupt(self, job: LongJob) -> None: ...
    async def archive(self, job: LongJob) -> None: ...
    async def settle(self, job: LongJob) -> None: ...


async def require_admission(
    sm: async_sessionmaker[AsyncSession], job: LongJob, *, session: AsyncSession | None = None
) -> None:
    if await is_over_balance(sessionmaker=sm, tenant_id=job.tenant_id, session=session):
        raise StoreError("Long job refused: tenant balance depleted")
    if await is_over_cap(
        billing_config=None,
        sessionmaker=sm,
        tenant_id=job.tenant_id,
        user_id=job.owner_external_user_id,
        now=datetime.now(UTC),
        session=session,
    ):
        raise StoreError("Long job refused: owner monthly cap exceeded")


class LongJobExecutor:
    """Keep leases alive while driving; shutdown leaves the durable worker reclaimable."""

    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        port: WorkerPort,
        ceilings: LongJobsSettings,
        *,
        clock: Callable[[], datetime] | None = None,
        heartbeat_s: float = 20,
    ) -> None:
        self.sm, self.port, self.ceilings = sm, port, ceilings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.heartbeat_s = heartbeat_s
        self.owner = str(uuid.uuid4())
        self.tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def sweep(self) -> None:
        for tenant in await list_tenants_by_platform(self.sm):
            if tenant.archived_at is not None:
                continue
            async with self.sm.begin() as db:
                jobs = await store.claim(
                    db,
                    tenant_id=tenant.id,
                    owner=self.owner,
                    now=self.clock(),
                    ceilings=self.ceilings,
                )
            for job in jobs:
                if job.id in self.tasks and not self.tasks[job.id].done():
                    continue
                self.tasks[job.id] = asyncio.create_task(self._guarded(job))
        self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}

    async def _guarded(self, job: LongJob) -> None:
        try:
            await self.execute(job)
        except Exception:
            # Transport/DB outages leave a lease, not a guessed terminal outcome.
            log.exception("long_job.executor_failed", job_id=str(job.id))

    def deadline(self, job: LongJob) -> datetime | None:
        if job.deadline_at is None:
            return None
        launch = job.deadline_at - timedelta(minutes=job.deadline_minutes)
        return min(job.deadline_at, launch + timedelta(minutes=self.ceilings.max_deadline_minutes))

    async def execute(self, job: LongJob) -> None:
        current = job
        deadline = self.deadline(job)
        drive: asyncio.Task[None] | None = None
        outcome: LongJobState | None = job.stop_state
        manifest = LongJobManifest(exit_code=None, summary=job.summary or "")
        cost = Decimal("0")
        try:
            if outcome is None and current.cancel_requested:
                outcome = "cancelled"
            elif outcome is None and deadline is not None and self.clock() >= deadline:
                outcome = "timed_out"
            if (
                outcome is None
                and current.worker_session_id is None
                and not current.cancel_requested
            ):
                try:
                    await require_admission(self.sm, current)
                    async with self.sm.begin() as db:
                        policy = await store.get_policy(
                            db,
                            tenant_id=job.tenant_id,
                            agent_id=job.agent_id,
                            ceilings=self.ceilings,
                        )
                    store.validate_limits(
                        policy, deadline_minutes=job.deadline_minutes, max_usd=job.max_usd
                    )
                except StoreError as exc:
                    outcome = "failed"
                    manifest = LongJobManifest(exit_code=None, summary=str(exc))
                if outcome is None:
                    worker_id = await self.port.create_or_find(current)
                    async with self.sm.begin() as db:
                        saved = await store.save_worker(db, current, worker_session_id=worker_id)
                    if saved is None:
                        return
                    current = saved
            if outcome is None and deadline is not None and self.clock() >= deadline:
                outcome = "timed_out"
            if outcome is None and current.worker_session_id is not None:
                resumed = await self.port.has_started(current)
                async with self.sm.begin() as db:
                    saved = await store.transition(db, current, state="running")
                if saved is None:
                    return
                current = saved
                # No send if already cancelled or past its retained deadline.
                if not current.cancel_requested and (deadline is None or self.clock() < deadline):
                    drive = asyncio.create_task(self.port.drive(current, resume=resumed))
            while outcome is None:
                async with self.sm.begin() as db:
                    live = await store.get(
                        db,
                        current.id,
                        tenant_id=current.tenant_id,
                        owner_account_id=current.owner_account_id,
                    )
                    cost = await store.session_cost(db, current)
                if (
                    live is None
                    or live.version != current.version
                    or live.lease_owner != self.owner
                ):
                    return
                current = live
                if current.cancel_requested:
                    outcome = "cancelled"
                elif deadline is not None and self.clock() >= deadline:
                    outcome = "timed_out"
                elif cost > min(current.max_usd, self.ceilings.max_usd):
                    outcome = "over_budget"
                elif drive is not None and drive.done():
                    await drive
                    await self.port.settle(current)
                    async with self.sm.begin() as db:
                        cost = await store.session_cost(db, current)
                    if cost > min(current.max_usd, self.ceilings.max_usd):
                        outcome = "over_budget"
                    else:
                        found = await self.port.manifest(current)
                        if deadline is not None and self.clock() >= deadline:
                            outcome = "timed_out"
                        elif found is None:
                            outcome = "lost"
                            manifest = LongJobManifest(
                                exit_code=None, summary="Worker ended without a completion manifest"
                            )
                        else:
                            manifest = found
                            outcome = "succeeded" if found.exit_code == 0 else "failed"
                if outcome is not None:
                    break
                async with self.sm.begin() as db:
                    saved = await store.heartbeat(db, current, now=self.clock())
                if saved is None:
                    return
                current = saved
                if drive is None:
                    raise WorkerVanished()
                await asyncio.wait({drive}, timeout=self.heartbeat_s)
        except WorkerBudgetReached:
            outcome = "over_budget"
        except WorkerVanished as exc:
            outcome = "lost"
            manifest = LongJobManifest(
                exit_code=None, summary=str(exc)[:4000] or "Worker session vanished"
            )
        finally:
            if drive is not None and not drive.done():
                drive.cancel()
                await asyncio.gather(drive, return_exceptions=True)
        assert outcome is not None
        if (
            outcome in ("cancelled", "timed_out", "over_budget", "lost")
            and current.worker_session_id
        ):
            if current.stop_state is None:
                async with self.sm.begin() as db:
                    saved = await store.transition(
                        db, current, stop_state=outcome, summary=manifest.summary
                    )
                if saved is None:
                    return
                current = saved
            # Interrupt alone does not stop a detached process. Do not publish
            # the terminal outcome until archive has also succeeded.
            if current.interrupted_at is None and current.archived_at is None:
                async with self.sm.begin() as db:
                    saved = await store.transition(db, current, interrupt_attempted_at=self.clock())
                if saved is None:
                    return
                current = saved
                try:
                    await self.port.interrupt(current)
                except Exception as exc:
                    # Archive is the stop boundary even when interrupt's stream
                    # cannot acknowledge the request. Never let an ack timeout
                    # prevent the sandbox teardown.
                    log.warning(
                        "long_job.interrupt_unconfirmed",
                        job_id=str(current.id),
                        error_type=type(exc).__name__,
                    )
                    manifest = LongJobManifest(
                        exit_code=None,
                        summary="Interrupt was not acknowledged; the worker session was archived.",
                    )
                else:
                    async with self.sm.begin() as db:
                        saved = await store.transition(db, current, interrupted_at=self.clock())
                    if saved is None:
                        return
                    current = saved
            if current.archived_at is None:
                try:
                    await self.port.archive(current)
                except WorkerVanished:
                    outcome = "lost"
                    manifest = LongJobManifest(
                        exit_code=None, summary="Worker session vanished during stop"
                    )
                else:
                    async with self.sm.begin() as db:
                        saved = await store.transition(db, current, archived_at=self.clock())
                    if saved is None:
                        return
                    current = saved
        if current.worker_session_id:
            try:
                await self.port.settle(current)
            except WorkerVanished:
                outcome = "lost"
            async with self.sm.begin() as db:
                cost = await store.session_cost(db, current)
        async with self.sm.begin() as db:
            await store.finish(
                db, current, state=outcome, now=self.clock(), manifest=manifest, cost=cost
            )

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
