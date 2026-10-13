"""Mirrors and their outbox (`daimon.core.mirrors`, `daimon.core.mirror_delivery`).

A delivery is posted at most once: claim (with a lease) → post → settle. A
claim whose lease ran out is settled `skipped/interrupted`, never handed out
again. Its payload (the question and answer) is dropped when it settles.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from daimon.core._models import Mirror, MirrorDelivery, Tenant
from daimon.core.mirror_events import ConversationEventV1
from daimon.core.stores.domain import MirrorDeliveryRow, MirrorRow, RoutineDestinationKind
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased


async def upsert_mirror(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    source_agent_id: uuid.UUID,
    source_agent_name: str,
    source_agent_names: Sequence[str],
    dest_platform: str,
    dest_kind: RoutineDestinationKind,
    dest_id: str,
    consent_note: str | None,
    max_answer_chars: int | None,
    account_id: uuid.UUID | None,
    now: datetime,
) -> MirrorRow:
    """Create the tenant's mirror `name`, or replace it and make it active again.

    A consent note records attested consent by `account_id` at `now`. A
    replacement with another agent or destination settles the posts queued
    under the old one (`replaced`): they were matched and checked for it.
    """
    old = await session.scalar(
        select(Mirror).where(Mirror.tenant_id == tenant_id, Mirror.name == name).with_for_update()
    )
    target = (source_agent_id, dest_platform, dest_kind, dest_id)
    if (
        old is not None
        and (old.source_agent_id, old.dest_platform, old.dest_kind, old.dest_id) != target
    ):
        await session.execute(
            update(MirrorDelivery)
            .where(
                MirrorDelivery.mirror_id == old.id,
                MirrorDelivery.status.in_(("pending", "claimed")),
            )
            .values(_settled("replaced", now))
        )
    values: dict[str, Any] = {
        "source_agent_id": source_agent_id,
        "source_agent_name": source_agent_name,
        "source_agent_names": list(source_agent_names),
        "dest_tenant_id": tenant_id,
        "dest_platform": dest_platform,
        "dest_kind": dest_kind,
        "dest_id": dest_id,
        "consent_basis": "attested" if consent_note else None,
        "consent_note": consent_note or None,
        "consent_by_account_id": account_id if consent_note else None,
        "consent_at": now if consent_note else None,
        "status": "active",
        "paused_reason": None,
        "max_answer_chars": max_answer_chars,
        "updated_at": now,
    }
    statement = (
        pg_insert(Mirror)
        .values(tenant_id=tenant_id, name=name, created_by_account_id=account_id, **values)
        .on_conflict_do_update(constraint="uq_mirrors_tenant_name", set_=values)
        .returning(Mirror)
    )
    row = (await session.execute(statement)).scalar_one()
    return MirrorRow.model_validate(row)


async def list_for_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, *, active_only: bool = False
) -> list[MirrorRow]:
    query = select(Mirror).where(Mirror.tenant_id == tenant_id).order_by(Mirror.name)
    if active_only:
        query = query.where(Mirror.status == "active")
    return [MirrorRow.model_validate(row) for row in (await session.scalars(query)).all()]


async def get_mirror(session: AsyncSession, mirror_id: uuid.UUID) -> MirrorRow | None:
    row = await session.get(Mirror, mirror_id)
    return MirrorRow.model_validate(row) if row is not None else None


async def delete_mirror(session: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> bool:
    """Delete the mirror and, by cascade, its outbox rows."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(Mirror).where(Mirror.tenant_id == tenant_id, Mirror.name == name)
        ),
    )
    return result.rowcount == 1


async def mark_paused(
    session: AsyncSession, mirror_id: uuid.UUID, *, reason: str, now: datetime
) -> None:
    await session.execute(
        update(Mirror)
        .where(Mirror.id == mirror_id)
        .values(status="paused", paused_reason=reason, updated_at=now)
    )


async def insert_deliveries(
    session: AsyncSession,
    *,
    event: ConversationEventV1,
    mirror_ids: Sequence[uuid.UUID],
    now: datetime,
) -> int:
    """Queue `event` for each mirror; an event already queued for one is not
    queued twice. Returns how many rows were added."""
    if not mirror_ids:
        return 0
    payload = event.model_dump(mode="json")
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            pg_insert(MirrorDelivery)
            .values(
                [
                    {
                        "tenant_id": event.tenant_id,
                        "mirror_id": mirror_id,
                        "event_id": event.id,
                        "source_session_key": event.source.session_key,
                        "source_account_id": event.actor.account_id,
                        "payload": payload,
                        "created_at": now,
                    }
                    for mirror_id in mirror_ids
                ]
            )
            .on_conflict_do_nothing(constraint="uq_mirror_deliveries_event")
        ),
    )
    return result.rowcount


def _settled(note: str, now: datetime) -> dict[str, Any]:
    return {
        "status": "skipped",
        "note": note,
        "payload": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "settled_at": now,
    }


async def settle_unpostable_deliveries(
    session: AsyncSession, *, now: datetime, max_pending_age: timedelta
) -> int:
    """Settle what must not post, in every tenant (archived ones too) and for
    every platform: claims whose lease ran out (`interrupted`, the owner may
    have posted), posts older than `max_pending_age` (`stale`), and posts of a
    paused mirror (`paused`). Returns how many."""
    paused = select(Mirror.id).where(Mirror.status == "paused")
    pending = MirrorDelivery.status == "pending"
    settled = 0
    for condition, note in (
        (
            (MirrorDelivery.status == "claimed", MirrorDelivery.lease_expires_at < now),
            "interrupted",
        ),
        ((pending, MirrorDelivery.created_at < now - max_pending_age), "stale"),
        ((pending, MirrorDelivery.mirror_id.in_(paused)), "paused"),
    ):
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(MirrorDelivery).where(*condition).values(_settled(note, now))
            ),
        )
        settled += result.rowcount
    return settled


async def claim_mirror_deliveries(
    session: AsyncSession,
    *,
    platform: str,
    owner: str,
    now: datetime,
    lease: timedelta,
    max_pending_age: timedelta,
    limit: int = 20,
) -> list[MirrorDeliveryRow]:
    """Claim due posts for mirrors into `platform`, oldest first.

    First settles what must not post (`settle_unpostable_deliveries`). Then
    claims at most one post per (mirror, session), and none for a pair that
    already has one claimed, so a session's posts keep their order and its
    thread is opened once. Archived tenants' posts wait until they are stale.
    """
    await settle_unpostable_deliveries(session, now=now, max_pending_age=max_pending_age)
    live = select(Tenant.id).where(Tenant.platform == platform, Tenant.archived_at.is_(None))
    here = select(Mirror.id).where(Mirror.dest_platform == platform, Mirror.tenant_id.in_(live))
    pending = (MirrorDelivery.status == "pending", MirrorDelivery.mirror_id.in_(here))
    claimed = aliased(MirrorDelivery)
    busy = exists().where(
        claimed.mirror_id == MirrorDelivery.mirror_id,
        claimed.source_session_key == MirrorDelivery.source_session_key,
        claimed.status == "claimed",
    )
    first_per_session = (
        select(MirrorDelivery.id)
        .where(*pending, ~busy)
        .distinct(MirrorDelivery.mirror_id, MirrorDelivery.source_session_key)
        .order_by(
            MirrorDelivery.mirror_id,
            MirrorDelivery.source_session_key,
            MirrorDelivery.created_at,
            MirrorDelivery.id,
        )
    )
    due = (
        select(MirrorDelivery.id)
        .where(MirrorDelivery.id.in_(first_per_session))
        .order_by(MirrorDelivery.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(
        update(MirrorDelivery)
        .where(MirrorDelivery.id.in_(due), MirrorDelivery.status == "pending")
        .values(status="claimed", lease_owner=owner, lease_expires_at=now + lease)
        .returning(MirrorDelivery)
        .execution_options(synchronize_session=False)
    )
    rows = [MirrorDeliveryRow.model_validate(row) for row in result.scalars().all()]
    return sorted(rows, key=lambda row: row.created_at)


async def extend_mirror_claim(
    session: AsyncSession, delivery_id: uuid.UUID, *, owner: str, now: datetime, lease: timedelta
) -> bool:
    """Renew a claim `owner` still holds unexpired. False: it was lost, do not post."""
    renewed = await session.scalar(
        update(MirrorDelivery)
        .where(
            MirrorDelivery.id == delivery_id,
            MirrorDelivery.status == "claimed",
            MirrorDelivery.lease_owner == owner,
            MirrorDelivery.lease_expires_at >= now,
        )
        .values(lease_expires_at=now + lease)
        .returning(MirrorDelivery.id)
    )
    return renewed is not None


async def settle_mirror_delivery(
    session: AsyncSession,
    delivery_id: uuid.UUID,
    *,
    owner: str,
    status: Literal["delivered", "skipped"],
    now: datetime,
    note: str | None = None,
    dest_thread_id: str | None = None,
) -> bool:
    """Finish a claim `owner` still holds and drop its payload. False: the claim was lost."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(MirrorDelivery)
            .where(
                MirrorDelivery.id == delivery_id,
                MirrorDelivery.status == "claimed",
                MirrorDelivery.lease_owner == owner,
            )
            .values(
                status=status,
                note=note,
                payload=None,
                lease_owner=None,
                lease_expires_at=None,
                dest_thread_id=dest_thread_id,
                settled_at=now,
            )
        ),
    )
    return result.rowcount == 1


async def get_session_thread(
    session: AsyncSession, *, mirror_id: uuid.UUID, session_key: str
) -> str | None:
    """The thread the session's last delivered post went to, if any."""
    return await session.scalar(
        select(MirrorDelivery.dest_thread_id)
        .where(
            MirrorDelivery.mirror_id == mirror_id,
            MirrorDelivery.source_session_key == session_key,
            MirrorDelivery.status == "delivered",
            MirrorDelivery.dest_thread_id.is_not(None),
        )
        .order_by(MirrorDelivery.created_at.desc())
        .limit(1)
    )


async def is_mirror_thread(session: AsyncSession, *, thread_id: str) -> bool:
    """Whether a mirror opened `thread_id` for a session's posts."""
    return bool(
        await session.scalar(select(exists().where(MirrorDelivery.dest_thread_id == thread_id)))
    )


async def count_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).where(MirrorDelivery.source_account_id == account_id)
        )
    ) or 0


async def delete_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Delete the person's queued and settled posts. Messages already posted stay."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(MirrorDelivery).where(MirrorDelivery.source_account_id == account_id)
        ),
    )
    return result.rowcount
