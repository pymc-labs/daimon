"""Store for `channel_admins`: who administers a channel on top of the server admins.

A row lists role ids and user ids; no row means nobody extra. Ids are the
platform's own (Discord snowflakes, Slack ids) and are stored sorted and
de-duplicated so two writes of the same set compare equal.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from daimon.core._models import ChannelAdmin
from daimon.core.stores.domain import ChannelAdminsRow
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def list_channel_admins(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str
) -> list[ChannelAdminsRow]:
    """Every channel with admins in this tenant, ordered by channel id."""
    stmt = (
        select(ChannelAdmin)
        .where(ChannelAdmin.tenant_id == tenant_id, ChannelAdmin.platform == platform)
        .order_by(ChannelAdmin.channel_id.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [ChannelAdminsRow.model_validate(row) for row in rows]


async def get_channel_admins(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> ChannelAdminsRow | None:
    orm = await session.get(ChannelAdmin, (tenant_id, platform, channel_id))
    return ChannelAdminsRow.model_validate(orm) if orm is not None else None


async def set_channel_admins(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    role_ids: Sequence[str],
    user_ids: Sequence[str],
    actor_account_id: uuid.UUID | None,
) -> ChannelAdminsRow:
    """Replace one channel's admins (upsert). The caller validates the ids."""
    values = {
        "tenant_id": tenant_id,
        "platform": platform,
        "channel_id": channel_id,
        "role_ids": sorted(set(role_ids)),
        "user_ids": sorted(set(user_ids)),
        "updated_by_account_id": actor_account_id,
    }
    stmt = (
        insert(ChannelAdmin)
        .values(**values)
        .on_conflict_do_update(
            constraint="pk_channel_admins",
            set_={
                "role_ids": values["role_ids"],
                "user_ids": values["user_ids"],
                "updated_by_account_id": actor_account_id,
                "updated_at": func.now(),
            },
        )
        .returning(ChannelAdmin)
    )
    # populate_existing: a row already in the identity map must come back as written.
    orm = (await session.execute(stmt, execution_options={"populate_existing": True})).scalar_one()
    await session.flush()
    return ChannelAdminsRow.model_validate(orm)


async def delete_channel_admins(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> bool:
    """Remove one channel's admins. True when a row existed."""
    result = await session.execute(
        delete(ChannelAdmin).where(
            ChannelAdmin.tenant_id == tenant_id,
            ChannelAdmin.platform == platform,
            ChannelAdmin.channel_id == channel_id,
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount > 0


async def list_administered_channel_ids(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    role_ids: Sequence[str],
) -> frozenset[str]:
    """The channels whose grant names the member, by user id or role."""
    stmt = select(ChannelAdmin.channel_id).where(
        ChannelAdmin.tenant_id == tenant_id,
        ChannelAdmin.platform == platform,
        or_(
            ChannelAdmin.user_ids.contains([platform_user_id]),
            ChannelAdmin.role_ids.overlap(list(role_ids)),
        ),
    )
    return frozenset((await session.execute(stmt)).scalars())


async def remove_user_from_channel_admins(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> int:
    """Erase one member's user id from every channel's admins. Returns rows touched.

    A row left with no roles and no users grants nothing, so it is deleted
    rather than kept as an empty shell.
    """
    result = await session.execute(
        update(ChannelAdmin)
        .where(
            ChannelAdmin.tenant_id == tenant_id,
            ChannelAdmin.platform == platform,
            ChannelAdmin.user_ids.contains([platform_user_id]),
        )
        .values(user_ids=func.array_remove(ChannelAdmin.user_ids, platform_user_id))
    )
    touched = cast(CursorResult[Any], result).rowcount
    await session.execute(
        delete(ChannelAdmin).where(
            ChannelAdmin.tenant_id == tenant_id,
            ChannelAdmin.platform == platform,
            func.cardinality(ChannelAdmin.user_ids) == 0,
            func.cardinality(ChannelAdmin.role_ids) == 0,
        )
    )
    await session.flush()
    return touched


async def count_channel_admin_grants_for_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> int:
    """How many channels list this member's user id: what erasure would touch."""
    stmt = (
        select(func.count())
        .select_from(ChannelAdmin)
        .where(
            ChannelAdmin.tenant_id == tenant_id,
            ChannelAdmin.platform == platform,
            ChannelAdmin.user_ids.contains([platform_user_id]),
        )
    )
    return int((await session.execute(stmt)).scalar_one())
