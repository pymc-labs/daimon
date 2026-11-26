"""Which scoped skills a chat turn may mount: those whose data scope its whole audience holds.

A library skill limited to a scope that an agent or channel carries mounts only on a turn
where everyone it reaches holds the scope, by the data audience rule
(`daimon.core.data_audience`): the asker and the agent's owner, and in a room every viewer,
named. Anything unknown mounts none: a contained or held turn, a helper's turn, data reads
off, a room whose viewers are not stored (only Discord's are; the data tools read Slack and
Teams rooms live), or a failed read. The skill tools filter by who is present on their own.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

import structlog
from daimon.core.config import Settings
from daimon.core.data_audience import (
    CAP,
    DataOrigin,
    DataRefusal,
    Reader,
    data_audience,
    user_subject,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ports.data_scopes import held_scopes
from daimon.core.stores import workspace_skills
from daimon.core.stores.agent_owners import owner_of
from daimon.core.stores.identity import accounts_of
from daimon.core.stores.room_viewers import read_viewers
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TurnPlace:
    """Where a turn runs and who asks, as admission resolved it."""

    tenant_id: uuid.UUID
    platform: str
    channel_id: str
    thread_id: str | None
    asker_user_id: str | None
    asker_account_id: uuid.UUID | None
    ma_agent_id: str
    private: bool
    """A DM or the web chat: the asker alone reads it."""
    closed: bool
    """No data may be read here: contained, held, a helper's turn or data reads off."""


async def scoped_skill_audience(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    place: TurnPlace,
    skill_ids: Collection[str],
    *,
    now: datetime,
) -> tuple[dict[str, str], frozenset[str]]:
    """The scope of each of `skill_ids` limited to one, and the scopes the turn's audience
    holds; the audience is read only when a skill is scoped."""
    async with sessions() as session:
        scoped = await workspace_skills.scopes_among(
            session, tenant_id=place.tenant_id, skill_ids=skill_ids
        )
        if not scoped or place.closed or settings is None:
            return scoped, frozenset()
        origin = await _origin(session, place, now=now)
        owner = await owner_of(
            session,
            place.tenant_id,
            derive_agent_uuid(tenant_id=place.tenant_id, ma_agent_id=place.ma_agent_id),
        )
    found = None if origin is None else data_audience(origin, agent_owner=owner)
    if found is None or isinstance(found, DataRefusal):
        return scoped, frozenset()
    try:
        return scoped, await held_scopes(settings, place.tenant_id, found)
    # Boundary: a scope that can't be checked mounts nothing, and the turn still runs.
    except Exception:
        log.warning("skill_audience.unreadable", tenant_id=str(place.tenant_id), exc_info=True)
        return scoped, frozenset()


async def _origin(session: AsyncSession, place: TurnPlace, *, now: datetime) -> DataOrigin | None:
    """The turn's data origin; None when its readers can't be named."""
    tenant = await get_tenant(session, place.tenant_id)
    if tenant is None or place.asker_user_id is None:
        return None
    workspace = tenant.external_id
    asker = Reader(
        user_subject(place.platform, workspace, place.asker_user_id), place.asker_account_id
    )
    if place.private:
        return DataOrigin("private", asker)
    if place.platform != "discord":
        return None
    stored = await read_viewers(
        session, tenant_id=place.tenant_id, channel_id=place.thread_id or place.channel_id, now=now
    )
    if stored is None or len(stored) > CAP:
        return None
    prefix = user_subject(place.platform, workspace, "")
    users = {s.removeprefix(prefix) for s in stored}
    linked = await accounts_of(
        session, tenant_id=place.tenant_id, platform=place.platform, external_ids=users
    )
    viewers = frozenset(
        Reader(user_subject(place.platform, workspace, user), linked.get(user)) for user in users
    )
    return DataOrigin("room", asker, viewers)
