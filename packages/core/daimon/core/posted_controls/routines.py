"""The routine panel's re-read, permission check and pause/resume transition."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol

from daimon.core.stores.domain import RoutineRow
from sqlalchemy.ext.asyncio import AsyncSession

RoutineAction = Literal["pause", "resume", "toggle"]
RoutineResult = Literal["changed", "gone", "wrong_tenant", "not_owner"]


class LoadOrPause(Protocol):
    async def __call__(
        self, session: AsyncSession, routine_id: uuid.UUID, *, tenant_id: uuid.UUID
    ) -> RoutineRow | None: ...


class Resume(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        routine_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        now: datetime,
    ) -> RoutineRow | None: ...


async def apply_routine_action(
    session: AsyncSession,
    routine_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    op: RoutineAction,
    allowed: Callable[[RoutineRow], Awaitable[bool]],
    load: LoadOrPause,
    pause: LoadOrPause,
    resume: Resume,
    now: Callable[[], datetime],
    check_tenant: bool = False,
) -> RoutineResult:
    """Run inside the caller's transaction; keep Discord's current-row toggle.

    Actor resolution and store functions are supplied by the adapter. The
    Discord-only defensive tenant check precedes its authority callback.
    """
    row = await load(session, routine_id, tenant_id=tenant_id)
    if row is None:
        return "gone"
    if check_tenant and row.tenant_id != tenant_id:
        return "wrong_tenant"
    if not await allowed(row):
        return "not_owner"
    if op == "toggle":
        op = "pause" if row.enabled else "resume"
    if op == "pause":
        await pause(session, routine_id, tenant_id=tenant_id)
    else:
        await resume(session, routine_id, tenant_id=tenant_id, now=now())
    return "changed"
