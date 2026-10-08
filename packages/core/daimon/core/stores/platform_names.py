"""Last-known names of a tenant's people and channels, as their platform gave them.

A person's name is only stored while they have a platform principal in that
tenant: the write locks their principal row, and a privacy purge deletes the
name after the principal, so a write racing the purge either lands first and is
deleted with it or finds no principal and stores nothing. Someone with no
principal (no account here, or one erased) therefore never has a stored name,
which keeps `/privacy`'s "no data on file" true.

No try/except: exceptions propagate to the caller, which for a name is the
best-effort recorder in `daimon.core.platform_names`. Callers own the
transaction; every write ends with `await session.flush()`.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from daimon.core._models import PlatformChannelName, PlatformPrincipal, PlatformUserName
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class KnownName:
    """What a platform calls a person: a display name, a handle, or both."""

    display_name: str | None = None
    handle: str | None = None

    @property
    def label(self) -> str | None:
        """The display name, else the handle; None when neither is known."""
        return self.display_name or self.handle


def _clean(value: object) -> str | None:
    """One line of text, or None for a blank name or anything that is not text."""
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text or None


async def upsert_user_names(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    names: Mapping[str, KnownName],
) -> int:
    """Store the name of each person who has a principal here; return how many were stored.

    A part not given (None) keeps the stored one. Blank names are dropped, a
    person with neither part is skipped, and so is anyone without a principal
    in this tenant on this platform (see the module docstring).
    """
    wanted = sorted(user_id for user_id in names if user_id)
    if not wanted:
        return 0
    known = set(
        await session.scalars(
            select(PlatformPrincipal.external_id)
            .where(
                PlatformPrincipal.tenant_id == tenant_id,
                PlatformPrincipal.platform == platform,
                PlatformPrincipal.external_id.in_(wanted),
            )
            .with_for_update(read=True)
        )
    )
    now = datetime.now(UTC)
    values: list[dict[str, object]] = []
    for user_id in wanted:
        name = names[user_id]
        display, handle = _clean(name.display_name), _clean(name.handle)
        if user_id in known and (display is not None or handle is not None):
            values.append(
                {
                    "tenant_id": tenant_id,
                    "platform": platform,
                    "platform_user_id": user_id,
                    "display_name": display,
                    "handle": handle,
                    "updated_at": now,
                }
            )
    if not values:
        return 0
    stmt = pg_insert(PlatformUserName).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            PlatformUserName.tenant_id,
            PlatformUserName.platform,
            PlatformUserName.platform_user_id,
        ],
        set_={
            "display_name": func.coalesce(
                stmt.excluded.display_name, PlatformUserName.display_name
            ),
            "handle": func.coalesce(stmt.excluded.handle, PlatformUserName.handle),
            "updated_at": now,
        },
    )
    await session.execute(stmt)
    await session.flush()
    return len(values)


async def get_user_names(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_ids: Collection[str],
) -> dict[str, KnownName]:
    """The stored name of each of ``user_ids`` that has one."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(
            PlatformUserName.platform_user_id,
            PlatformUserName.display_name,
            PlatformUserName.handle,
        ).where(
            PlatformUserName.tenant_id == tenant_id,
            PlatformUserName.platform == platform,
            PlatformUserName.platform_user_id.in_(list(user_ids)),
        )
    )
    return {user_id: KnownName(display, handle) for user_id, display, handle in rows.all()}


async def delete_user_names_for_platform_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> int:
    """Forget one person's stored name in one tenant. Idempotent."""
    result = await session.execute(
        delete(PlatformUserName).where(
            PlatformUserName.tenant_id == tenant_id,
            PlatformUserName.platform == platform,
            PlatformUserName.platform_user_id == platform_user_id,
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount


async def count_user_names_for_platform_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> int:
    """How many rows `delete_user_names_for_platform_user` would delete (0 or 1)."""
    stmt = (
        select(func.count())
        .select_from(PlatformUserName)
        .where(
            PlatformUserName.tenant_id == tenant_id,
            PlatformUserName.platform == platform,
            PlatformUserName.platform_user_id == platform_user_id,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def upsert_channel_names(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    names: Mapping[str, str],
) -> None:
    """Store each channel's name; blank names are skipped."""
    now = datetime.now(UTC)
    values = [
        {
            "tenant_id": tenant_id,
            "platform": platform,
            "channel_id": channel_id,
            "name": clean,
            "updated_at": now,
        }
        for channel_id, name in sorted(names.items())
        if channel_id and (clean := _clean(name)) is not None
    ]
    if not values:
        return
    stmt = pg_insert(PlatformChannelName).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            PlatformChannelName.tenant_id,
            PlatformChannelName.platform,
            PlatformChannelName.channel_id,
        ],
        set_={"name": stmt.excluded.name, "updated_at": now},
    )
    await session.execute(stmt)
    await session.flush()


async def get_channel_names(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_ids: Collection[str],
) -> dict[str, str]:
    """The stored name of each of ``channel_ids`` that has one."""
    if not channel_ids:
        return {}
    rows = await session.execute(
        select(PlatformChannelName.channel_id, PlatformChannelName.name).where(
            PlatformChannelName.tenant_id == tenant_id,
            PlatformChannelName.platform == platform,
            PlatformChannelName.channel_id.in_(list(channel_ids)),
        )
    )
    return {channel_id: name for channel_id, name in rows.all()}
