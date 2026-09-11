"""Async store for organic thread participation: scope modes and the auto-response ledger.

Callers own the transaction; every write ends with `await session.flush()`.
No try/except -- exceptions propagate to the adapter boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from daimon.core._models import ThreadAutoResponse, ThreadParticipationScope
from daimon.core.stores.domain import ThreadAutoResponseRow
from daimon.core.thread_participation import ParticipationMode, ParticipationScope
from sqlalchemy import CursorResult, delete, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

# The workspace row needs no id of its own: tenant_id already names it.
_WORKSPACE_SCOPE_ID = ""


def _scope_id(scope: ParticipationScope, scope_id: str | None) -> str:
    if scope is ParticipationScope.WORKSPACE:
        return _WORKSPACE_SCOPE_ID
    if scope_id is None:
        raise ValueError(f"{scope.value} scope needs a scope_id")
    return scope_id


@dataclass(frozen=True)
class ParticipationModes:
    """The explicit settings at each tier, `None` where nothing was set."""

    workspace: ParticipationMode | None
    channel: ParticipationMode | None
    thread: ParticipationMode | None


async def get_participation_modes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    thread_id: str | None,
) -> ParticipationModes:
    """One read for the whole cascade: the workspace row plus this channel's and thread's rows."""
    keys: list[tuple[str, str]] = [(ParticipationScope.WORKSPACE.value, _WORKSPACE_SCOPE_ID)]
    if channel_id is not None:
        keys.append((ParticipationScope.CHANNEL.value, channel_id))
    if thread_id is not None:
        keys.append((ParticipationScope.THREAD.value, thread_id))
    rows = (
        await session.execute(
            select(ThreadParticipationScope.scope, ThreadParticipationScope.mode).where(
                ThreadParticipationScope.tenant_id == tenant_id,
                ThreadParticipationScope.platform == platform,
                or_(
                    *(
                        tuple_(ThreadParticipationScope.scope, ThreadParticipationScope.scope_id)
                        == k
                        for k in keys
                    )
                ),
            )
        )
    ).all()
    found = {scope: ParticipationMode(mode) for scope, mode in rows}
    return ParticipationModes(
        workspace=found.get(ParticipationScope.WORKSPACE.value),
        channel=found.get(ParticipationScope.CHANNEL.value),
        thread=found.get(ParticipationScope.THREAD.value),
    )


async def set_participation_mode(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: ParticipationScope,
    scope_id: str | None,
    mode: ParticipationMode,
) -> None:
    stmt = (
        pg_insert(ThreadParticipationScope)
        .values(
            tenant_id=tenant_id,
            platform=platform,
            scope=scope.value,
            scope_id=_scope_id(scope, scope_id),
            mode=mode.value,
            updated_at=func.now(),
        )
        .on_conflict_do_update(
            constraint="pk_thread_participation_scopes",
            set_={"mode": mode.value, "updated_at": func.now()},
        )
    )
    await session.execute(stmt)
    await session.flush()


async def clear_participation_mode(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: ParticipationScope,
    scope_id: str | None,
) -> bool:
    """Drop the explicit setting so the scope inherits again. Returns whether a row existed."""
    result = await session.execute(
        delete(ThreadParticipationScope).where(
            ThreadParticipationScope.tenant_id == tenant_id,
            ThreadParticipationScope.platform == platform,
            ThreadParticipationScope.scope == scope.value,
            ThreadParticipationScope.scope_id == _scope_id(scope, scope_id),
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount == 1


async def record_auto_response(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    message_id: str,
    created_at: datetime | None = None,
) -> ThreadAutoResponseRow:
    """Append one ledger row. `created_at` exists so tests can backdate; production omits it.

    Two constructor calls rather than a kwargs splat: omitting `created_at`
    entirely is what lets the column default apply, and both calls stay
    type-checked.
    """
    orm = (
        ThreadAutoResponse(
            tenant_id=tenant_id, platform=platform, thread_id=thread_id, message_id=message_id
        )
        if created_at is None
        else ThreadAutoResponse(
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            message_id=message_id,
            created_at=created_at,
        )
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return ThreadAutoResponseRow.model_validate(orm)


async def count_auto_responses_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    since: datetime,
) -> int:
    """Unprompted replies in this thread at or after `since`: the rows are the rate-limit ledger."""
    return (
        await session.execute(
            select(func.count())
            .select_from(ThreadAutoResponse)
            .where(
                ThreadAutoResponse.tenant_id == tenant_id,
                ThreadAutoResponse.platform == platform,
                ThreadAutoResponse.thread_id == thread_id,
                ThreadAutoResponse.created_at >= since,
            )
        )
    ).scalar_one()
