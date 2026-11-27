"""The scope check of shared connections: a call holds a scope when everyone it answers for
does, the ceiling data reads use. Closed without the store."""

from __future__ import annotations

import uuid

import psycopg
from daimon.core.config import Settings
from daimon.core.data_audience import DataAudience, scope_ceiling
from daimon.core.ports.gate import data_sources_on
from daimon.data import access, identity, pool
from daimon.data.schema import Schemas


async def holds_scope(
    settings: Settings, tenant_id: uuid.UUID, audience: DataAudience, scope: str
) -> bool:
    if not data_sources_on(settings) or not await pool.ready(settings):
        return False
    schemas = Schemas.of(settings.data)
    try:
        async with pool.connection(settings) as conn:
            holders = await identity.holders_of(conn, schemas, tenant_id, list(audience.people))
            scopes_of = {
                account: frozenset(await access.effective_scopes(conn, schemas, tenant_id, held))
                for account, held in holders.items()
            }
    except psycopg.Error:
        return False  # an unreadable store holds no scope
    return scope in scope_ceiling(audience, scopes_of)


__all__ = ["holds_scope"]
