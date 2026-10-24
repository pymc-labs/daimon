"""Exactly-once admission of Teams messages, across deliveries, restarts and drains.

Each step is one conditional write under the row lock, so two processes (or a
replay and a redelivery) never both win: `claim_teams_message` admits once or
reclaims a dead owner's unhandled row; `finish_teams_messages` is the fence
before a turn starts or any answer is sent; `mark_restart_notice` lets a drain
notice go out once; `reclaim_teams_messages` hands a boot what a dead process
left unhandled; `list_recent_teams_channels` names where a restart may have
missed some. Owner liveness is the process's advisory lock, as for Discord.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from daimon.core._models import TeamsMessageAdmission, Tenant
from daimon.core.stores.domain import TeamsMessageAdmissionRow
from daimon.core.stores.worker_ownership import owner_is_alive
from pydantic import JsonValue
from sqlalchemy import CursorResult, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


def _keys(tenant_id: uuid.UUID, channel_id: str, message_ids: Collection[str]) -> tuple[Any, ...]:
    return (
        TeamsMessageAdmission.tenant_id == tenant_id,
        TeamsMessageAdmission.channel_id == channel_id,
        TeamsMessageAdmission.message_id.in_(tuple(message_ids)),
    )


async def claim_teams_message(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    message_id: str,
    conversation_id: str,
    owner_key: int,
    sent_at: datetime,
    user_id: str | None = None,
    message: dict[str, JsonValue] | None = None,
) -> bool:
    """Admit a message once, or reclaim it from a process that exited unhandled.

    `channel_id` is the channel (a thread's, without `;messageid=`) or the chat,
    and `message_id` the activity id: a Graph notification for the same channel
    message carries the same pair, so either delivery path collides here. With
    no `message` the row is handled at once (nothing to replay); with one it is
    kept until `finish_teams_messages`. With no tenant row there is nothing to
    record against: True, and admission refuses the message.
    """
    if await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id)) is None:
        return True
    inserted = await session.scalar(
        insert(TeamsMessageAdmission)
        .values(
            tenant_id=tenant_id,
            channel_id=channel_id,
            message_id=message_id,
            conversation_id=conversation_id,
            user_id=user_id,
            owner_key=owner_key,
            handled=message is None,
            message=message,
            sent_at=sent_at,
        )
        .on_conflict_do_nothing()
        .returning(TeamsMessageAdmission.message_id)
    )
    if inserted is not None:
        return True
    row = await session.scalar(
        select(TeamsMessageAdmission)
        .where(*_keys(tenant_id, channel_id, [message_id]))
        .with_for_update()
    )
    if row is None or row.handled or row.owner_key == owner_key:
        return False
    if await owner_is_alive(session, row.owner_key):
        return False
    # A new owner's drain owes the chat its own notice.
    row.owner_key, row.notice_sent = owner_key, False
    await session.flush()
    return True


async def finish_teams_messages(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    message_ids: Collection[str],
    owner_key: int,
) -> set[str]:
    """Mark this owner's messages handled and drop their content, before the turn or answer.

    One transaction for a whole batch. Returns the ids the caller may act on:
    its own, and any with no row (a form submit); one another process owns is
    left alone and left out. Idempotent for this owner.
    """
    rows = (
        await session.execute(
            select(TeamsMessageAdmission.message_id, TeamsMessageAdmission.owner_key)
            .where(*_keys(tenant_id, channel_id, message_ids))
            .with_for_update()
        )
    ).all()
    taken = {row.message_id for row in rows if row.owner_key != owner_key}
    ours = [row.message_id for row in rows if row.owner_key == owner_key]
    if ours:
        await session.execute(
            update(TeamsMessageAdmission)
            .where(*_keys(tenant_id, channel_id, ours))
            .values(handled=True, message=None)
        )
    return set(message_ids) - taken


async def list_unhandled_teams_messages(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    message_ids: Collection[str],
    owner_key: int,
) -> set[str]:
    """Which of `message_ids` this owner admitted and has not handled yet."""
    ids = await session.scalars(
        select(TeamsMessageAdmission.message_id).where(
            *_keys(tenant_id, channel_id, message_ids),
            TeamsMessageAdmission.owner_key == owner_key,
            TeamsMessageAdmission.handled.is_(False),
        )
    )
    return set(ids.all())


async def mark_restart_notice(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    conversation_id: str,
    message_ids: Collection[str],
    owner_key: int,
) -> bool:
    """Record the restart notice on unhandled messages; True if their chat is still owed one.

    One notice per chat per drain: False once any of this owner's unhandled
    messages there carries it. The chat's rows are locked, so two holds agree.
    """
    chat = (
        TeamsMessageAdmission.tenant_id == tenant_id,
        TeamsMessageAdmission.channel_id == channel_id,
        TeamsMessageAdmission.conversation_id == conversation_id,
        TeamsMessageAdmission.owner_key == owner_key,
        TeamsMessageAdmission.handled.is_(False),
    )
    told = await session.scalars(
        select(TeamsMessageAdmission.notice_sent).where(*chat).with_for_update()
    )
    owed = not any(told.all())
    marked = await session.execute(
        update(TeamsMessageAdmission)
        .where(
            *chat,
            TeamsMessageAdmission.message_id.in_(tuple(message_ids)),
            TeamsMessageAdmission.notice_sent.is_(False),
        )
        .values(notice_sent=True)
        .returning(TeamsMessageAdmission.message_id)
    )
    return owed and bool(marked.all())


@dataclass(frozen=True)
class ReclaimedRows:
    """A dead process's unhandled rows: to replay, and too old to (now handled)."""

    replay: list[TeamsMessageAdmissionRow] = field(default_factory=list[TeamsMessageAdmissionRow])
    stale: list[TeamsMessageAdmissionRow] = field(default_factory=list[TeamsMessageAdmissionRow])


async def reclaim_teams_messages(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_key: int,
    replay_after: datetime,
    horizon: datetime,
) -> ReclaimedRows:
    """Take every unhandled row a dead owner left since `horizon`, oldest sent first.

    Rows since `replay_after` move to `owner_key`, still unhandled, for replay.
    Older ones are handled here, before anyone is told: a lost notice is never
    repeated. A row a live owner holds, or another reclaim has locked, is skipped.
    """
    rows = (
        await session.scalars(
            select(TeamsMessageAdmission)
            .where(
                TeamsMessageAdmission.tenant_id == tenant_id,
                TeamsMessageAdmission.handled.is_(False),
                TeamsMessageAdmission.owner_key != owner_key,
                TeamsMessageAdmission.created_at >= horizon,
            )
            .order_by(TeamsMessageAdmission.sent_at, TeamsMessageAdmission.created_at)
            .with_for_update(skip_locked=True)
        )
    ).all()
    alive: dict[int, bool] = {}
    taken = ReclaimedRows()
    for row in rows:
        if row.owner_key not in alive:
            alive[row.owner_key] = await owner_is_alive(session, row.owner_key)
        if alive[row.owner_key]:
            continue
        before = TeamsMessageAdmissionRow.model_validate(row)
        fresh = row.created_at >= replay_after
        row.owner_key, row.notice_sent = owner_key, False
        if not fresh:
            row.handled, row.message = True, None
        (taken.replay if fresh else taken.stale).append(before)
    await session.flush()
    return taken


async def list_recent_teams_channels(
    session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime, limit: int
) -> list[str]:
    """The channels and chats with a message admitted since `since`, newest activity first."""
    ids = await session.scalars(
        select(TeamsMessageAdmission.channel_id)
        .where(
            TeamsMessageAdmission.tenant_id == tenant_id, TeamsMessageAdmission.created_at >= since
        )
        .group_by(TeamsMessageAdmission.channel_id)
        .order_by(func.max(TeamsMessageAdmission.created_at).desc())
        .limit(limit)
    )
    return list(ids)


async def prune_teams_messages(
    session: AsyncSession, *, tenant_id: uuid.UUID, before: datetime, owner_key: int
) -> int:
    """Forget rows older than `before`: handled ones, and dead owners' unhandled ones."""
    old = (TeamsMessageAdmission.tenant_id == tenant_id, TeamsMessageAdmission.created_at < before)
    owners = (
        await session.scalars(
            select(TeamsMessageAdmission.owner_key)
            .where(*old, TeamsMessageAdmission.handled.is_(False))
            .distinct()
        )
    ).all()
    dead = [o for o in owners if o != owner_key and not await owner_is_alive(session, o)]
    result = await session.execute(
        delete(TeamsMessageAdmission).where(
            *old,
            or_(TeamsMessageAdmission.handled.is_(True), TeamsMessageAdmission.owner_key.in_(dead)),
        )
    )
    return cast(CursorResult[Any], result).rowcount


async def forget_teams_messages_for_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: str
) -> int:
    """Forget one person in one tenant's admission rows. Idempotent.

    The row stays as a content-free receipt (handled, no words, no sender) until
    the 24 h prune: it is the key that keeps a catch-up or a redelivery from
    running the same message again, which would write the erased words back.
    An unhandled message of theirs is dropped, not replayed.
    """
    result = await session.execute(
        update(TeamsMessageAdmission)
        .where(
            TeamsMessageAdmission.tenant_id == tenant_id, TeamsMessageAdmission.user_id == user_id
        )
        .values(user_id=None, message=None, handled=True)
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount


async def count_teams_messages_for_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: str
) -> int:
    """How many rows `forget_teams_messages_for_user` would clear."""
    stmt = (
        select(func.count())
        .select_from(TeamsMessageAdmission)
        .where(
            TeamsMessageAdmission.tenant_id == tenant_id, TeamsMessageAdmission.user_id == user_id
        )
    )
    return int((await session.execute(stmt)).scalar_one())
