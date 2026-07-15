"""Resolve invoker -> account_id and load PurgePreview for the cascade view.

`resolve_privacy_account` runs at handler entry and may return None (D-SCOPE-02);
the cog renders the grey deleted-state embed in that case. `load_purge_preview`
is called when the Delete… button is clicked.
"""

from __future__ import annotations

import uuid

from daimon.core.privacy import PurgePreview, collect_purge_preview
from daimon.core.stores import identity as identity_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def resolve_privacy_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> uuid.UUID | None:
    """Return the account_id for this Discord user, or None if no principal exists.

    D-SCOPE-02: must NOT create a principal on miss. Uses the read-only
    `find_platform_principal` helper.
    """
    principal = await identity_store.find_platform_principal(
        session,
        tenant_id=tenant_id,
        platform="discord",
        external_id=platform_user_id,
    )
    return None if principal is None else principal.account_id


async def load_purge_preview(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> PurgePreview:
    """Thin wrapper around daimon.core.privacy.collect_purge_preview."""
    return await collect_purge_preview(sm=session_factory, account_id=account_id)
