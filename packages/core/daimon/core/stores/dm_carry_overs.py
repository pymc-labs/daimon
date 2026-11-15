"""An earlier private conversation held until the person's first DM here."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from daimon.core._models import DirectMessageConversation, DmCarryOver
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, delete, func, literal_column, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

PutResult = Literal["new", "updated", "consumed"]


class CarryOver(BaseModel):
    """What a person's first private conversation starts from."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    history: list[dict[str, str]]
    context: str


async def put_carry_over(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    source_name: str,
    exported_at: datetime,
    history: list[dict[str, str]],
    context: str,
) -> PutResult:
    """Store or replace a waiting conversation; a taken one is never brought back."""
    values = {
        "source_name": source_name,
        "exported_at": exported_at,
        "history": history,
        "context": context,
    }
    inserted = (
        await session.execute(
            insert(DmCarryOver)
            .values(
                tenant_id=tenant_id, platform=platform, external_user_id=external_user_id, **values
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "platform", "external_user_id"],
                set_={**values, "imported_at": func.now()},
                where=DmCarryOver.consumed_at.is_(None),
            )
            # xmax is zero only on a row this statement inserted.
            .returning(literal_column("(xmax = 0)"))
        )
    ).first()
    if inserted is None:
        return "consumed"
    return "new" if inserted[0] else "updated"


async def has_conversation(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, external_user_id: str
) -> bool:
    """True when this person already has a private conversation here."""
    found = await session.scalar(
        select(DirectMessageConversation.scope_id)
        .where(
            DirectMessageConversation.tenant_id == tenant_id,
            DirectMessageConversation.platform == platform,
            DirectMessageConversation.external_user_id == external_user_id,
        )
        .limit(1)
    )
    return found is not None


async def take_carry_over(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, external_user_id: str
) -> CarryOver | None:
    """Take the waiting conversation once. It keeps only a marker afterwards."""
    row = await session.scalar(
        select(DmCarryOver)
        .where(
            DmCarryOver.tenant_id == tenant_id,
            DmCarryOver.platform == platform,
            DmCarryOver.external_user_id == external_user_id,
            DmCarryOver.consumed_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        return None
    taken = CarryOver(source_name=row.source_name, history=row.history, context=row.context)
    row.consumed_at = func.now()
    row.history = []
    row.context = ""
    await session.flush()
    return taken


def _keys(users: Sequence[tuple[uuid.UUID, str, str]]) -> ColumnElement[bool]:
    return tuple_(DmCarryOver.tenant_id, DmCarryOver.platform, DmCarryOver.external_user_id).in_(
        list(users)
    )


async def count_for_users(
    session: AsyncSession, *, users: Sequence[tuple[uuid.UUID, str, str]]
) -> int:
    """Rows held for these (tenant, platform, user) keys."""
    if not users:
        return 0
    return (
        await session.execute(select(func.count()).select_from(DmCarryOver).where(_keys(users)))
    ).scalar_one()


async def delete_for_users(
    session: AsyncSession, *, users: Sequence[tuple[uuid.UUID, str, str]]
) -> int:
    """Delete rows held for these (tenant, platform, user) keys."""
    if not users:
        return 0
    removed = (
        await session.execute(
            delete(DmCarryOver).where(_keys(users)).returning(DmCarryOver.external_user_id)
        )
    ).all()
    return len(removed)
