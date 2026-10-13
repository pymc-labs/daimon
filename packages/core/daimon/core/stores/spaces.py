"""Store for `spaces` and `space_channels`: which channels share one audience and rule.

A channel is in at most one Space (the primary key). Members carry their own
tenant, but a Space takes only channels of the tenant that owns it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, cast

from daimon.core._models import Account, Space, SpaceChannel, Tenant, ThreadSession
from daimon.core.access_policy import SPACE_KEY_PREFIX, ChannelRule, SpaceRule
from daimon.core.channel_ref import ChannelRef
from daimon.core.errors import UserFacingError
from daimon.core.stores.domain import Platform, SpaceRow
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


class SpaceRefused(UserFacingError):
    """A Space change was refused; the message is person-facing."""


def space_key(space_id: uuid.UUID) -> str:
    return f"{SPACE_KEY_PREFIX}{space_id}"


async def create_space(
    session: AsyncSession, *, tenant_id: uuid.UUID, name: str, rule: ChannelRule
) -> SpaceRow:
    """Raise `SpaceRefused` when the tenant already has a Space of that name."""
    if await get_space(session, tenant_id=tenant_id, name=name) is not None:
        raise SpaceRefused(f"there is already a Space named {name}")
    row = Space(tenant_id=tenant_id, name=name, readers=rule.readers, writers=rule.writers)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return SpaceRow.model_validate(row)


async def get_space(session: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> SpaceRow | None:
    row = (
        await session.execute(select(Space).where(Space.tenant_id == tenant_id, Space.name == name))
    ).scalar_one_or_none()
    return SpaceRow.model_validate(row) if row is not None else None


async def list_spaces_for_tenant(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[SpaceRow]:
    rows = (
        await session.execute(
            select(Space).where(Space.tenant_id == tenant_id).order_by(Space.name)
        )
    ).scalars()
    return [SpaceRow.model_validate(row) for row in rows]


async def list_space_channels(
    session: AsyncSession, *, tenant_id: uuid.UUID, space_id: uuid.UUID
) -> list[ChannelRef]:
    """The channels of `tenant_id`'s Space, each with its own tenant and platform, by id."""
    rows = await session.execute(
        select(SpaceChannel.tenant_id, Tenant.platform, SpaceChannel.channel_id)
        .join(Tenant, Tenant.id == SpaceChannel.tenant_id)
        .join(Space, Space.id == SpaceChannel.space_id)
        .where(SpaceChannel.space_id == space_id, Space.tenant_id == tenant_id)
        .order_by(SpaceChannel.channel_id)
    )
    return [
        ChannelRef(tenant_id=tenant_id, platform=cast("Platform", platform), channel_id=channel_id)
        for tenant_id, platform, channel_id in rows.tuples()
    ]


async def get_channel_space_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str
) -> uuid.UUID | None:
    return (
        await session.execute(
            select(SpaceChannel.space_id).where(
                SpaceChannel.tenant_id == tenant_id, SpaceChannel.channel_id == channel_id
            )
        )
    ).scalar_one_or_none()


async def add_space_channel(
    session: AsyncSession, *, space: SpaceRow, tenant_id: uuid.UUID, channel_id: str
) -> None:
    """Raise `SpaceRefused` for another tenant's channel or one already in a Space."""
    # Members from another tenant need the place/agent tenant split first.
    if tenant_id != space.tenant_id:
        raise SpaceRefused("a Space takes only channels of the workspace that owns it")
    if held := await get_channel_space_id(session, tenant_id=tenant_id, channel_id=channel_id):
        if held == space.id:
            raise SpaceRefused(f"{channel_id} is already in Space {space.name}")
        raise SpaceRefused(f"{channel_id} is already in a Space; remove it from that one first")
    try:
        async with session.begin_nested():
            session.add(SpaceChannel(tenant_id=tenant_id, channel_id=channel_id, space_id=space.id))
    except IntegrityError as exc:
        raise SpaceRefused(f"{channel_id} is already in a Space") from exc


async def remove_space_channel(
    session: AsyncSession, *, space_id: uuid.UUID, tenant_id: uuid.UUID, channel_id: str
) -> bool:
    """True when the channel was in this Space."""
    result = await session.execute(
        delete(SpaceChannel).where(
            SpaceChannel.space_id == space_id,
            SpaceChannel.tenant_id == tenant_id,
            SpaceChannel.channel_id == channel_id,
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount > 0


async def delete_space(session: AsyncSession, *, tenant_id: uuid.UUID, space_id: uuid.UUID) -> bool:
    """Delete `tenant_id`'s Space and its memberships; True when it existed."""
    result = await session.execute(
        delete(Space).where(Space.id == space_id, Space.tenant_id == tenant_id)
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount > 0


async def list_space_rules(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, SpaceRule]:
    """Every Space a channel of `tenant_id` is in, by key, with that tenant's channels."""
    rows = await session.execute(
        select(Space.id, Space.name, Space.readers, Space.writers, SpaceChannel.channel_id)
        .join(SpaceChannel, SpaceChannel.space_id == Space.id)
        .where(SpaceChannel.tenant_id == tenant_id)
    )
    found: dict[str, tuple[str, ChannelRule, set[str]]] = {}
    for space_id, name, readers, writers, channel_id in rows.tuples():
        rule = ChannelRule.model_validate({"readers": readers, "writers": writers})
        found.setdefault(space_key(space_id), (name, rule, set()))[2].add(channel_id)
    return {
        key: SpaceRule(key=key, name=name, rule=rule, channel_ids=frozenset(ids))
        for key, (name, rule, ids) in found.items()
    }


async def count_external_accounts_seen(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_ids: Collection[str]
) -> dict[str, int]:
    """Per channel, how many accounts from another organisation had a conversation there."""
    rows = await session.execute(
        select(ThreadSession.channel_id, func.count(func.distinct(Account.id)))
        .join(Account, Account.id == ThreadSession.account_id)
        .where(
            ThreadSession.tenant_id == tenant_id,
            ThreadSession.channel_id.in_(list(channel_ids)),
            Account.is_external.is_(True),
        )
        .group_by(ThreadSession.channel_id)
    )
    counts = {channel_id: 0 for channel_id in channel_ids}
    for channel_id, count in rows.tuples():
        if channel_id is not None:
            counts[channel_id] = count
    return counts
