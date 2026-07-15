"""Pure scheduler decision functions. Loop/lifecycle lives in adapters/scheduler/."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

import structlog
from daimon.core.ids import generate_request_id
from daimon.core.observability import capture_exception_with_scope
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import (
    advance_stale,
    claim_due_fireable,
    record_result,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class CapsCheck(Protocol):
    async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str) -> bool: ...


FireFn = Callable[[RoutineRow], Awaitable[None]]


async def _record_fire_error(
    sm: async_sessionmaker[AsyncSession], routine_id: uuid.UUID, msg: str
) -> None:
    """Record a fire failure on a FRESH session (D-04 — never reuse fire's session)."""
    try:
        async with sm() as s, s.begin():
            await record_result(s, routine_id, tail=None, error=msg)
    except Exception:
        log.exception("record_result(error) failed", routine_id=str(routine_id))


async def run_one_tick(
    *,
    now: datetime,
    sm: async_sessionmaker[AsyncSession],
    caps: CapsCheck,
    fire: FireFn,
    max_age: timedelta,
    max_concurrent_fires: int,
    dispatch_timeout_s: float,
) -> None:
    """One scheduler tick. All collaborators injected; no module-level state.

    Decision flow:
      1. advance_stale (recover orphans + stale rows)
      2. claim_due_fireable (atomic 2-phase claim)
      3. Pre-gather cap check: skip capped rows, build fireable list
      4. await asyncio.gather(*(_fire_guarded(r) for r in fireable))
         Each guarded member acquires the semaphore, wraps fire(row) in
         asyncio.wait_for(timeout=dispatch_timeout_s), catches TimeoutError /
         Exception internally, and returns None — gather never raises.
    """
    async with sm() as session, session.begin():
        try:
            await advance_stale(session, now=now, max_age=max_age)
        except Exception:
            log.exception("advance_stale failed")
        try:
            rows = await claim_due_fireable(session, now=now, max_age=max_age)
        except Exception:
            log.exception("claim_due_fireable failed")
            return

    # Pre-gather sequential cap check (D-05 / Open-Q1 recommendation: cheaper
    # than checking inside the semaphore boundary; keeps the existing cap path).
    fireable: list[RoutineRow] = []
    for row in rows:
        if row.created_by_user_id is None:
            # No user to bill against — treat as uncapped (DM exemption D-03 applies
            # symmetrically to routines without an attributable owner).
            over = False
        else:
            try:
                over = await caps.is_over_cap(row.tenant_id, row.created_by_user_id)
            except Exception:
                log.exception("caps.is_over_cap failed", routine_id=str(row.id))
                continue
        if over:
            try:
                async with sm() as s, s.begin():
                    await record_result(s, row.id, tail=None, error="cap_exceeded")
            except Exception:
                log.exception("record_result(cap-block) failed", routine_id=str(row.id))
            continue
        fireable.append(row)

    # Build semaphore FRESH inside run_one_tick — no module/global state (DI rule).
    sem = asyncio.Semaphore(max_concurrent_fires)

    async def _fire_guarded(row: RoutineRow) -> None:
        # Per-fire correlation context (D-05): every log line emitted inside this
        # fire — claim/advance/record-result and the turn body — carries a fresh
        # rid and the row's tenant_id. Unbind in finally so the context is clean
        # even on the error paths.
        rid = generate_request_id()
        structlog.contextvars.bind_contextvars(rid=rid, tenant_id=str(row.tenant_id))
        try:
            async with sem:
                try:
                    await asyncio.wait_for(fire(row), timeout=dispatch_timeout_s)
                except TimeoutError as err:
                    # OB-4 (D-09): capture to Sentry, then keep the existing swallow —
                    # gather never raises and record_result still runs.
                    capture_exception_with_scope(err)
                    await _record_fire_error(sm, row.id, f"timeout: exceeded {dispatch_timeout_s}s")
                except Exception as err:
                    capture_exception_with_scope(err)
                    await _record_fire_error(sm, row.id, f"{type(err).__name__}: {err}"[:500])
        finally:
            structlog.contextvars.unbind_contextvars("rid", "tenant_id")

    # Pitfall 2: spawn each fire as its own task so asyncio.create_task copies the
    # current contextvars context at creation — concurrent fires then bind into
    # ISOLATED contexts and never cross-contaminate rids. Bare coroutines in gather
    # would share one context (the semaphore alone does NOT fix this).
    await asyncio.gather(*(asyncio.create_task(_fire_guarded(r)) for r in fireable))

    log.info(
        "scheduler.tick",
        claimed=len(rows),
        fired=len(fireable),
    )
