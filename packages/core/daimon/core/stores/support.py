"""Support menus, menu places, limits and own channels: tenant config, read at request time.

No row is the default: no place limit is `settings.support.credits_per_user`
each, no menu place is the next wider place and then `support.DEFAULT_MENU`, no
person row is the limit's `each_person`, no channel row is the deployment's.
Callers own the transaction and the tenant lock; exceptions propagate.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import (
    SUPPORT_REVISION_SEQ,
    SupportChannel,
    SupportLimit,
    SupportMenu,
    SupportMenuPlace,
)
from daimon.core.stores.support_rows import (
    SupportChannelRow,
    SupportLimitRow,
    SupportMenuPlaceRow,
    SupportMenuRow,
    SupportSetup,
)
from daimon.core.support import (
    SUPPORT_SCOPES,
    MenuSpec,
    SupportPlace,
    SupportScope,
    SupportWindow,
    place_scope_id,
)
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    delete,
    exists,
    func,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


def place_keys(place: SupportPlace) -> list[tuple[SupportScope, str]]:
    """(scope, scope_id) of every place that holds `place`, most specific first."""
    return [
        (scope, scope_id)
        for scope in SUPPORT_SCOPES
        if (scope_id := place_scope_id(scope, place)) is not None
    ]


# ---------- Menus ----------


async def get_menu(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> SupportMenuRow | None:
    stmt = select(SupportMenu).where(SupportMenu.tenant_id == tenant_id, SupportMenu.key == key)
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportMenuRow.model_validate(orm)


async def get_menu_by_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, menu_id: uuid.UUID
) -> SupportMenuRow | None:
    stmt = select(SupportMenu).where(SupportMenu.tenant_id == tenant_id, SupportMenu.id == menu_id)
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportMenuRow.model_validate(orm)


async def list_menus(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[SupportMenuRow]:
    stmt = select(SupportMenu).where(SupportMenu.tenant_id == tenant_id).order_by(SupportMenu.key)
    return [SupportMenuRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


async def set_menu(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    spec: MenuSpec,
    set_by_account_id: uuid.UUID | None,
) -> SupportMenuRow:
    """Insert or replace the menu under a new `revision`, so open menus go stale.

    Every set takes the next number of one never-decreasing sequence, so no
    revision comes back after a menu is deleted and recreated.
    """
    options = [o.model_dump(mode="json", exclude_none=True) for o in spec.options]
    channel = None if spec.channel is None else spec.channel.model_dump(mode="json")
    values = {
        "name": spec.name,
        "options": options,
        "channel": channel,
        "set_by_account_id": set_by_account_id,
    }
    insert = pg_insert(SupportMenu).values(
        tenant_id=tenant_id, key=key, revision=SUPPORT_REVISION_SEQ.next_value(), **values
    )
    stmt = (
        insert.on_conflict_do_update(
            constraint="uq_support_menus_tenant_key",
            # The number drawn for the insert, so one set takes one number.
            set_={**values, "revision": insert.excluded.revision, "updated_at": func.now()},
        )
        .returning(SupportMenu)
        .execution_options(populate_existing=True)
    )
    orm = (await session.execute(stmt)).scalar_one()
    await session.flush()
    return SupportMenuRow.model_validate(orm)


async def delete_menu(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> SupportMenuRow | None:
    """Remove the menu; the places' RESTRICT key refuses one in use (`menu_places_using`)."""
    stmt = (
        delete(SupportMenu)
        .where(SupportMenu.tenant_id == tenant_id, SupportMenu.key == key)
        .returning(SupportMenu)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else SupportMenuRow.model_validate(orm)


# ---------- Menu places ----------


async def menu_places_using(
    session: AsyncSession, *, tenant_id: uuid.UUID, menu_id: uuid.UUID
) -> list[SupportMenuPlaceRow]:
    stmt = (
        select(SupportMenuPlace)
        .where(SupportMenuPlace.tenant_id == tenant_id, SupportMenuPlace.menu_id == menu_id)
        .order_by(SupportMenuPlace.scope, SupportMenuPlace.scope_id)
    )
    return [SupportMenuPlaceRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


async def get_menu_use(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope: SupportScope, scope_id: str
) -> SupportMenuPlaceRow | None:
    stmt = select(SupportMenuPlace).where(
        SupportMenuPlace.tenant_id == tenant_id,
        SupportMenuPlace.scope == scope,
        SupportMenuPlace.scope_id == scope_id,
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportMenuPlaceRow.model_validate(orm)


async def list_menu_uses(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[SupportMenuPlaceRow]:
    stmt = (
        select(SupportMenuPlace)
        .where(SupportMenuPlace.tenant_id == tenant_id)
        .order_by(SupportMenuPlace.scope, SupportMenuPlace.scope_id)
    )
    return [SupportMenuPlaceRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


async def set_menu_use(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scope: SupportScope,
    scope_id: str,
    menu_id: uuid.UUID,
    set_by_account_id: uuid.UUID | None,
) -> SupportMenuPlaceRow:
    values = {"menu_id": menu_id, "set_by_account_id": set_by_account_id}
    stmt = (
        pg_insert(SupportMenuPlace)
        .values(tenant_id=tenant_id, scope=scope, scope_id=scope_id, **values)
        .on_conflict_do_update(
            index_elements=["tenant_id", "scope", "scope_id"],
            set_={**values, "updated_at": func.now()},
        )
        .returning(SupportMenuPlace)
        .execution_options(populate_existing=True)
    )
    orm = (await session.execute(stmt)).scalar_one()
    await session.flush()
    return SupportMenuPlaceRow.model_validate(orm)


async def clear_menu_use(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope: SupportScope, scope_id: str
) -> SupportMenuPlaceRow | None:
    stmt = (
        delete(SupportMenuPlace)
        .where(
            SupportMenuPlace.tenant_id == tenant_id,
            SupportMenuPlace.scope == scope,
            SupportMenuPlace.scope_id == scope_id,
        )
        .returning(SupportMenuPlace)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else SupportMenuPlaceRow.model_validate(orm)


async def menu_for_place(
    session: AsyncSession, *, tenant_id: uuid.UUID, place: SupportPlace
) -> SupportMenuRow | None:
    """The menu of the most specific place that names one; None is the built-in menu."""
    keys = place_keys(place)
    stmt = (
        select(SupportMenuPlace.scope, SupportMenu)
        .join(SupportMenu, SupportMenu.id == SupportMenuPlace.menu_id)
        .where(
            SupportMenuPlace.tenant_id == tenant_id,
            tuple_(SupportMenuPlace.scope, SupportMenuPlace.scope_id).in_(keys),
        )
    )
    by_scope = {scope: menu for scope, menu in (await session.execute(stmt)).all()}
    for scope, _ in keys:
        if (menu := by_scope.get(scope)) is not None:
            return SupportMenuRow.model_validate(menu)
    return None


# ---------- Limits ----------


def _limit_at(
    tenant_id: uuid.UUID, scope: SupportScope, scope_id: str, platform_user_id: str | None
) -> list[ColumnElement[bool]]:
    person = (
        SupportLimit.platform_user_id.is_(None)
        if platform_user_id is None
        else SupportLimit.platform_user_id == platform_user_id
    )
    return [
        SupportLimit.tenant_id == tenant_id,
        SupportLimit.scope == scope,
        SupportLimit.scope_id == scope_id,
        person,
    ]


async def get_limit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scope: SupportScope,
    scope_id: str,
    platform_user_id: str | None = None,
) -> SupportLimitRow | None:
    """The place's limit, or with `platform_user_id` that person's own limit there."""
    stmt = select(SupportLimit).where(*_limit_at(tenant_id, scope, scope_id, platform_user_id))
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportLimitRow.model_validate(orm)


async def get_limit_by_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit_id: uuid.UUID
) -> SupportLimitRow | None:
    stmt = select(SupportLimit).where(
        SupportLimit.tenant_id == tenant_id, SupportLimit.id == limit_id
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportLimitRow.model_validate(orm)


async def list_limits(
    session: AsyncSession, *, tenant_id: uuid.UUID, people: bool | None = None
) -> list[SupportLimitRow]:
    """Every limit; `people` True keeps person rows only, False place rows only."""
    stmt = select(SupportLimit).where(SupportLimit.tenant_id == tenant_id)
    if people is not None:
        column = SupportLimit.platform_user_id
        stmt = stmt.where(column.is_not(None) if people else column.is_(None))
    stmt = stmt.order_by(SupportLimit.scope, SupportLimit.scope_id, SupportLimit.platform_user_id)
    return [SupportLimitRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


async def limits_for_place(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    place: SupportPlace,
    platform_user_id: str | None = None,
) -> list[SupportLimitRow]:
    """Every place row that could govern `place`, and the person's own rows there."""
    person = SupportLimit.platform_user_id.is_(None)
    if platform_user_id is not None:
        person = person | (SupportLimit.platform_user_id == platform_user_id)
    stmt = select(SupportLimit).where(
        SupportLimit.tenant_id == tenant_id,
        tuple_(SupportLimit.scope, SupportLimit.scope_id).in_(place_keys(place)),
        person,
    )
    return [SupportLimitRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


async def set_limit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scope: SupportScope,
    scope_id: str,
    platform_user_id: str | None,
    shared: Decimal | None,
    each_person: Decimal | None,
    window: SupportWindow,
    starts_at: datetime | None,
    ends_at: datetime | None,
    set_by_account_id: uuid.UUID | None,
) -> SupportLimitRow:
    """Insert or replace one limit under a new `revision`; the table CHECKs refuse bad values."""
    values: dict[str, Any] = {
        "shared": shared,
        "each_person": each_person,
        "window": window,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "set_by_account_id": set_by_account_id,
    }
    existing = await get_limit(
        session,
        tenant_id=tenant_id,
        scope=scope,
        scope_id=scope_id,
        platform_user_id=platform_user_id,
    )
    if existing is None:
        stmt = pg_insert(SupportLimit).values(
            tenant_id=tenant_id,
            scope=scope,
            scope_id=scope_id,
            platform_user_id=platform_user_id,
            **values,
        )
    else:
        stmt = (
            update(SupportLimit)
            .where(SupportLimit.id == existing.id)
            .values(**values, revision=SUPPORT_REVISION_SEQ.next_value(), updated_at=func.now())
        )
    returning = stmt.returning(SupportLimit).execution_options(populate_existing=True)
    orm = (await session.execute(returning)).scalar_one()
    await session.flush()
    return SupportLimitRow.model_validate(orm)


async def delete_limit(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit_id: uuid.UUID
) -> SupportLimitRow | None:
    """Remove one limit; its requests keep their charges and count nowhere."""
    stmt = (
        delete(SupportLimit)
        .where(SupportLimit.tenant_id == tenant_id, SupportLimit.id == limit_id)
        .returning(SupportLimit)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else SupportLimitRow.model_validate(orm)


def _people(platform_user_keys: Sequence[tuple[uuid.UUID, str]]) -> ColumnElement[bool]:
    return tuple_(SupportLimit.tenant_id, SupportLimit.platform_user_id).in_(
        list(platform_user_keys)
    )


async def delete_person_limits_for_people(
    session: AsyncSession, *, platform_user_keys: Sequence[tuple[uuid.UUID, str]]
) -> int:
    """Erase people's own limits (purge); a person row names its person."""
    if not platform_user_keys:
        return 0
    result = await session.execute(delete(SupportLimit).where(_people(platform_user_keys)))
    return cast(CursorResult[Any], result).rowcount or 0


async def count_person_limits_for_people(
    session: AsyncSession, *, platform_user_keys: Sequence[tuple[uuid.UUID, str]]
) -> int:
    if not platform_user_keys:
        return 0
    stmt = select(func.count()).select_from(SupportLimit).where(_people(platform_user_keys))
    return int((await session.execute(stmt)).scalar_one())


# ---------- Own channel ----------


async def get_support_channel(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> SupportChannelRow | None:
    orm = await session.get(SupportChannel, tenant_id, populate_existing=True)
    return None if orm is None else SupportChannelRow.model_validate(orm)


async def get_support_setup(
    session: AsyncSession, *, tenant_id: uuid.UUID, place: SupportPlace
) -> SupportSetup:
    """The workspace's own channel platform, whether `place`'s menu names a channel, and
    whether any limit exists."""
    own = select(SupportChannel.platform).where(SupportChannel.tenant_id == tenant_id)
    # A person's limit gives that person credits (`_balance`), so it counts as one existing.
    limits = exists().where(SupportLimit.tenant_id == tenant_id)
    row = (await session.execute(select(own.scalar_subquery(), limits))).one()
    menu = await menu_for_place(session, tenant_id=tenant_id, place=place)
    return SupportSetup(
        own_platform=row[0],
        limits=bool(row[1]),
        menu_channel=menu is not None and menu.channel is not None,
    )


async def set_support_channel(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    slack_team_id: str | None = None,
    teams_team_id: str | None = None,
    teams_service_url: str | None = None,
    set_by_account_id: uuid.UUID | None,
) -> SupportChannelRow:
    values: dict[str, Any] = {
        "platform": platform,
        "channel_id": channel_id,
        "slack_team_id": slack_team_id,
        "teams_team_id": teams_team_id,
        "teams_service_url": teams_service_url,
        "set_by_account_id": set_by_account_id,
    }
    insert = pg_insert(SupportChannel).values(
        tenant_id=tenant_id, revision=SUPPORT_REVISION_SEQ.next_value(), **values
    )
    stmt = (
        insert.on_conflict_do_update(
            index_elements=["tenant_id"],
            set_={**values, "revision": insert.excluded.revision, "updated_at": func.now()},
        )
        .returning(SupportChannel)
        .execution_options(populate_existing=True)
    )
    orm = (await session.execute(stmt)).scalar_one()
    await session.flush()
    return SupportChannelRow.model_validate(orm)


async def clear_support_channel(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> SupportChannelRow | None:
    stmt = (
        delete(SupportChannel)
        .where(SupportChannel.tenant_id == tenant_id)
        .returning(SupportChannel)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else SupportChannelRow.model_validate(orm)


__all__ = [
    "clear_menu_use",
    "clear_support_channel",
    "count_person_limits_for_people",
    "delete_limit",
    "delete_menu",
    "delete_person_limits_for_people",
    "get_limit",
    "get_limit_by_id",
    "get_menu",
    "get_menu_by_id",
    "get_menu_use",
    "get_support_channel",
    "get_support_setup",
    "limits_for_place",
    "list_limits",
    "list_menu_uses",
    "list_menus",
    "menu_for_place",
    "menu_places_using",
    "place_keys",
    "set_limit",
    "set_menu",
    "set_menu_use",
    "set_support_channel",
]
