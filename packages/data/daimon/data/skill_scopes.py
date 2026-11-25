"""Which skill scopes a caller holds: the library's scoped skills ask the store's grants.

A person holds the scopes granted to their account or to an identity linked to it here
(`access.effective_scopes`); a caller with no person holds `public` at most, as an answer
for guests does (`data_audience.scope_ceiling`). Where the store is not set up there are
no grants, so a scoped skill stays hidden.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

import structlog
from daimon.core.config import Settings
from daimon.core.ports.data_subject import PUBLIC
from daimon.data import access, identity, pool
from daimon.data.schema import Schemas
from psycopg import Error

log = structlog.get_logger(__name__)


class GrantedScopes:
    """`SkillScopes` answered from the store's grants."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._schemas = Schemas.of(settings.data)

    async def visible(
        self, *, tenant_id: uuid.UUID, account_id: uuid.UUID | None, scopes: Collection[str]
    ) -> frozenset[str]:
        wanted = frozenset(scopes)
        if account_id is None:
            return wanted & {PUBLIC}
        try:
            async with pool.connection(self._settings) as conn:
                holders = await identity.list_holders(conn, self._schemas, tenant_id, account_id)
                held = await access.effective_scopes(conn, self._schemas, tenant_id, holders)
        except Error:
            log.warning("skill_scopes.read_failed", exc_info=True)
            return frozenset()
        return wanted & frozenset(held)
