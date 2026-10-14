"""Opt-in owned-DB scheduler ticks on the same clock as platform capture.

This runs product jobs. It neither creates native scheduled sessions nor
invents credential-card edits when the optional expiry binding is absent.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

from daimon.core.routine_delivery import RoutinePoster, delivery_target, poll_deliveries_once
from daimon.core.scheduler import CapsCheck, FireFn, RoutineDispatcher, run_one_tick
from daimon.core.stores.domain import CredentialRequestRow, RoutineRow
from daimon.core.stores.routines import get_routine
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordPostEvent, OfflineDiscordGateway
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.credential_form_expiry import EditExpiredCredentialCard


class LongWaitUnavailable(RuntimeError):
    """A named actual job is missing; no virtual time was advanced."""

    code = "CREDENTIAL_FORM_EXPIRY_JOB_UNAVAILABLE"


class RoutineJobClock:
    """A callable platform clock with an explicit UTC anchor, initially zero.

    Pass this same callable to HostClock/DiscordSurfaceHarness. Only the job
    runner advances it: time is a boundary for executing jobs, not evidence of
    completion, a warning, or delivery.
    """

    def __init__(self, *, anchor: datetime) -> None:
        if anchor.tzinfo is None or anchor.utcoffset() != timedelta(0):
            raise ValueError("routine clock requires an aware UTC anchor")
        self.anchor = anchor.astimezone(UTC)
        self._elapsed = 0.0

    def __call__(self) -> float:
        return self._elapsed

    def wall_time(self) -> datetime:
        return self.anchor + timedelta(seconds=self._elapsed)

    def _advance_to(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < self._elapsed:
            raise ValueError("routine clock must advance monotonically")
        self._elapsed = seconds


@dataclass(frozen=True)
class RoutineBinding:
    """IDs from the runner's actual routine creation/readback, never a text parse."""

    tenant_id: uuid.UUID
    routine_id: uuid.UUID


@dataclass(frozen=True)
class RoutineReadback:
    row: RoutineRow
    destination: Literal["saved_destination", "no_destination"]


@dataclass(frozen=True)
class CredentialFormExpiryBinding:
    """Explicit opt-in tenant and real adapter edit edge; no alternate DB bind."""

    tenant_id: uuid.UUID
    edit: EditExpiredCredentialCard
    limit: int = 20

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.tenant_id), uuid.UUID) or not callable(self.edit):
            raise ValueError("expiry requires an authorized tenant and adapter edit callback")
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ValueError("expiry batch must be between one and 100 requests")


@dataclass(frozen=True)
class CredentialFormExpiryCounts:
    """Actual poll counts only; the opaque request cursor is never published."""

    retired: int
    attempted: int
    acknowledged: int


@dataclass(frozen=True)
class RoutineTick:
    at_s: float
    at_utc: datetime
    readbacks: tuple[RoutineReadback, ...]
    delivery_claims: int
    posts: tuple[DiscordPostEvent, ...]
    credential_form_expiry: CredentialFormExpiryCounts | None = None


@dataclass(frozen=True)
class RoutineWaitReceipt:
    started_s: float
    finished_s: float
    ticks: tuple[RoutineTick, ...]
    # Always component evidence: SDK factory, trigger, root, warning and oracle
    # joins remain the consumer's responsibilities. No full-cell PASS marker.
    boundary: Literal["actual_scheduler_and_delivery_jobs", "actual_credential_form_expiry_job"] = (
        "actual_scheduler_and_delivery_jobs"
    )
    profile: Literal["opt_in_credential_form_expiry"] | None = None


class ClockedRoutineJobs:
    """Public long-wait hook: bounded chronological ticks, actual DB jobs/posts.

    `fire`, `caps` and `post` are the existing product scheduler/adapter edges,
    supplied by the owner/runner. A synthetic fire used by a unit test proves
    scheduling only, never native scheduled-session conformance. `bindings`
    join independently created durable rows; they do not seed or mutate rows.
    `gateway` is the actual offline Discord HTTP capture attached to the
    adapter client, sharing `clock` with its post observations.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        clock: RoutineJobClock,
        caps: CapsCheck,
        fire: FireFn,
        post: RoutinePoster,
        gateway: OfflineDiscordGateway,
        bindings: tuple[RoutineBinding, ...],
        tick_seconds: float = 15.0,
        credential_form_expiry: CredentialFormExpiryBinding | None = None,
    ) -> None:
        require_owned_sessionmaker(sessionmaker)
        if not math.isfinite(tick_seconds) or not 1 <= tick_seconds <= 15:
            raise ValueError("job tick must be between one and 15 seconds")
        if gateway.clock is not clock:
            raise ValueError("platform gateway must share the routine job clock")
        if len({b.routine_id for b in bindings}) != len(bindings):
            raise ValueError("independent, unique durable routine bindings are required")
        self._sm = sessionmaker
        self.clock = clock
        self._caps = caps
        self._fire = fire
        self._post = post
        self._gateway = gateway
        self._bindings = bindings
        self._tick_seconds = tick_seconds
        self._lock = asyncio.Lock()
        self._credential_form_expiry = credential_form_expiry
        self._expiry_cursor: str | None = None

    async def _readbacks(self) -> tuple[RoutineReadback, ...]:
        result: list[RoutineReadback] = []
        async with self._sm() as session:
            for binding in self._bindings:
                row = await get_routine(session, binding.routine_id, tenant_id=binding.tenant_id)
                if row is None:
                    raise ValueError("routine binding is absent or belongs to another tenant")
                target = delivery_target(row, platform="discord")
                result.append(
                    RoutineReadback(
                        row=row,
                        destination="no_destination" if target is None else "saved_destination",
                    )
                )
        return tuple(result)

    async def advance(
        self,
        seconds: float,
        *,
        job: Literal["routine", "credential_form_expiry"] = "routine",
    ) -> RoutineWaitReceipt:
        """Run due jobs at each <=15s boundary and drain before moving again.

        Missing expiry refuses BEFORE moving time or connecting. A failed or
        cancelled tick propagates; the runner cannot issue a completed wait.
        No new jobs/defaults or remote/live clients are created here.
        """
        if job == "credential_form_expiry" and self._credential_form_expiry is None:
            raise LongWaitUnavailable("No automatic credential-form expiry job exists")
        if job not in ("routine", "credential_form_expiry"):
            raise ValueError("unknown long-wait job")
        if not math.isfinite(seconds) or not 0 <= seconds <= 1920:
            raise ValueError("long wait must be finite and between zero and 1920 seconds")
        if self._lock.locked():
            raise ValueError("a routine clock advance is already running")
        async with self._lock:
            # Recheck routed binds before EACH invocation, not only construction.
            require_owned_sessionmaker(self._sm)
            if job == "credential_form_expiry":
                return await self._advance_expiry(seconds)
            await self._readbacks()
            started = self.clock()
            end = started + seconds
            ticks: list[RoutineTick] = []
            dispatcher = RoutineDispatcher(1, clock=self.clock.wall_time)
            try:
                while True:
                    before = len(self._gateway.events)
                    now = self.clock.wall_time()
                    await run_one_tick(
                        now=now,
                        sm=self._sm,
                        caps=self._caps,
                        fire=self._fire,
                        max_age=timedelta(minutes=15),
                        max_concurrent_fires=1,
                        dispatch_timeout_s=10.0,
                        dispatcher=dispatcher,
                        wait_for_completion=True,
                    )
                    claimed = await poll_deliveries_once(
                        self._sm,
                        platform="discord",
                        post=self._post,
                        now=now,
                        clock=self.clock.wall_time,
                    )
                    ticks.append(
                        RoutineTick(
                            at_s=self.clock(),
                            at_utc=now,
                            readbacks=await self._readbacks(),
                            delivery_claims=claimed,
                            posts=tuple(self._gateway.events[before:]),
                        )
                    )
                    if self.clock() == end:
                        break
                    self.clock._advance_to(  # pyright: ignore[reportPrivateUsage]
                        min(end, self.clock() + self._tick_seconds)
                    )
            finally:
                await dispatcher.close()
            return RoutineWaitReceipt(
                started_s=started, finished_s=self.clock(), ticks=tuple(ticks)
            )

    async def _advance_expiry(self, seconds: float) -> RoutineWaitReceipt:
        from daimon.core.credential_form_expiry import poll_credential_form_expiries_once

        binding = self._credential_form_expiry
        if binding is None:
            raise LongWaitUnavailable("No automatic credential-form expiry job exists")

        async def edit(row: CredentialRequestRow) -> bool:
            require_owned_sessionmaker(self._sm)
            acknowledged = await binding.edit(row)
            # Refuse callback routing mutations before returning an acknowledgement
            # that lets the product poller open its next database transaction.
            require_owned_sessionmaker(self._sm)
            return acknowledged

        started = self.clock()
        end = started + seconds
        ticks: list[RoutineTick] = []
        while True:
            # Callbacks may reconfigure a factory. Admit every routed engine
            # again immediately before the next real product poll invocation.
            require_owned_sessionmaker(self._sm)
            before = len(self._gateway.events)
            now = self.clock.wall_time()
            poll = await poll_credential_form_expiries_once(
                self._sm,
                now=now,
                edit=edit,
                tenant_id=binding.tenant_id,
                limit=binding.limit,
                after_token=self._expiry_cursor,
            )
            # The product poller catches callback exceptions, including guard
            # refusals. Re-admit outside that boundary even on the final tick.
            require_owned_sessionmaker(self._sm)
            self._expiry_cursor = poll.next_after_token
            ticks.append(
                RoutineTick(
                    at_s=self.clock(),
                    at_utc=now,
                    readbacks=(),
                    delivery_claims=0,
                    posts=tuple(self._gateway.events[before:]),
                    credential_form_expiry=CredentialFormExpiryCounts(
                        retired=poll.retired,
                        attempted=poll.attempted,
                        acknowledged=poll.acknowledged,
                    ),
                )
            )
            if self.clock() == end:
                break
            self.clock._advance_to(  # pyright: ignore[reportPrivateUsage]
                min(end, self.clock() + self._tick_seconds)
            )
        return RoutineWaitReceipt(
            started_s=started,
            finished_s=self.clock(),
            ticks=tuple(ticks),
            boundary="actual_credential_form_expiry_job",
            profile="opt_in_credential_form_expiry",
        )
