"""Which tenant a direct message belongs to.

A channel message names its tenant: the guild, workspace or organisation it
was posted in. A direct message does not. One Discord user can share several
daimon'd servers with the bot, so their DM could mean any of them.

The adapter lists the workspaces the sender shares with the bot. This module
narrows them to live tenants and returns the one the DM belongs to, or raises
`DmTenantSelectionRequired` so the adapter can render a picker. It runs before
`admit()`, so nothing is billed, no agent resolves and no session opens until
the person has picked. Copy and widgets stay adapter-side.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.dm_tenant_selections import (
    delete_dm_tenant_selection,
    get_dm_tenant_selection,
    set_dm_tenant_selection,
)
from daimon.core.stores.domain import Platform, TenantRow
from daimon.core.stores.tenants import get_tenants
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DmTenantCandidate(BaseModel):
    """A live tenant the sender shares with the bot."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    workspace_id: str


class NoDmTenantError(DaimonError):
    """The sender shares no live tenant with the bot."""


class DmTenantSelectionRequired(DaimonError):
    """Several tenants could own this DM and the sender has not picked one.

    Carries the candidates in the order the adapter listed their workspaces.
    """

    def __init__(self, candidates: tuple[DmTenantCandidate, ...]) -> None:
        super().__init__(f"{len(candidates)} tenants could own this direct message")
        self.candidates = candidates


def _is_live(row: TenantRow, *, platform: Platform) -> bool:
    return row.platform == platform and row.provision_status == "ready" and row.archived_at is None


def live_candidates(
    rows: Sequence[TenantRow], *, platform: Platform, workspace_ids: Sequence[str]
) -> tuple[DmTenantCandidate, ...]:
    """Keep the workspaces whose tenant is live, in the adapter's order, deduplicated."""
    live = {row.id for row in rows if _is_live(row, platform=platform)}
    candidates: dict[uuid.UUID, DmTenantCandidate] = {}
    for workspace_id in workspace_ids:
        tenant_id = derive_tenant_uuid(platform=platform, workspace_id=workspace_id)
        if tenant_id in live and tenant_id not in candidates:
            candidates[tenant_id] = DmTenantCandidate(
                tenant_id=tenant_id, workspace_id=workspace_id
            )
    return tuple(candidates.values())


def pick_dm_tenant(
    candidates: tuple[DmTenantCandidate, ...], *, selected: uuid.UUID | None
) -> DmTenantCandidate:
    """Pure decision: the only candidate, else the stored pick, else ask.

    A stored pick that is no longer a candidate (the person left that server,
    or it was archived) is ignored, so the picker comes back.
    """
    if not candidates:
        raise NoDmTenantError("no live tenant is shared with this person")
    if len(candidates) == 1:
        return candidates[0]
    for candidate in candidates:
        if candidate.tenant_id == selected:
            return candidate
    raise DmTenantSelectionRequired(candidates)


async def _candidates(
    session: AsyncSession, *, platform: Platform, workspace_ids: Sequence[str]
) -> tuple[DmTenantCandidate, ...]:
    ids = [derive_tenant_uuid(platform=platform, workspace_id=w) for w in workspace_ids]
    rows = await get_tenants(session, ids)
    return live_candidates(rows, platform=platform, workspace_ids=workspace_ids)


async def resolve_dm_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Platform,
    external_user_id: str,
    workspace_ids: Sequence[str],
) -> DmTenantCandidate:
    """Return the tenant this DM belongs to, or raise before any turn work.

    `external_user_id` must be platform-global (a Discord user id, an Entra
    object id): the stored pick follows the person, not a workspace.
    """
    async with sessionmaker() as session:
        candidates = await _candidates(session, platform=platform, workspace_ids=workspace_ids)
        selected = None
        if len(candidates) > 1:
            row = await get_dm_tenant_selection(
                session, platform=platform, external_user_id=external_user_id
            )
            selected = None if row is None else row.tenant_id
    return pick_dm_tenant(candidates, selected=selected)


async def choose_dm_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Platform,
    external_user_id: str,
    workspace_ids: Sequence[str],
    tenant_id: uuid.UUID,
) -> DmTenantCandidate:
    """Store a pick from the picker, re-checked against the live candidates.

    A stale pick (the tenant went away between render and click) raises
    `DmTenantSelectionRequired` with the current list, so the adapter can
    show the picker again.
    """
    async with sessionmaker() as session:
        candidates = await _candidates(session, platform=platform, workspace_ids=workspace_ids)
        chosen = pick_dm_tenant(candidates, selected=tenant_id)
        if chosen.tenant_id != tenant_id:
            raise DmTenantSelectionRequired(candidates)
        await set_dm_tenant_selection(
            session, platform=platform, external_user_id=external_user_id, tenant_id=tenant_id
        )
        await session.commit()
    return chosen


async def forget_dm_tenant(
    sessionmaker: async_sessionmaker[AsyncSession], *, platform: Platform, external_user_id: str
) -> bool:
    """Drop the stored pick so the next DM asks again. False when none was stored."""
    async with sessionmaker() as session:
        deleted = await delete_dm_tenant_selection(
            session, platform=platform, external_user_id=external_user_id
        )
        await session.commit()
    return deleted > 0
