"""Resolve invoker -> account_id and load PurgePreview for the Slack privacy panel.

`resolve_privacy_account` is read-only: uses `find_platform_principal` which NEVER
creates a principal on miss. `load_purge_preview` is a thin wrapper around the core
`collect_purge_preview` helper.

Port of packages/adapters/discord/daimon/adapters/discord/privacy_panel/read.py with
platform="slack" substituted for platform="discord".
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
    """Return the account_id for this Slack user, or None if no principal exists.

    Read-only: uses `find_platform_principal` which NEVER creates a principal on
    miss. The caller must NOT create a principal as a side effect of a /privacy
    lookup — the command is read-only until the user explicitly confirms deletion.
    """
    principal = await identity_store.find_platform_principal(
        session,
        tenant_id=tenant_id,
        platform="slack",
        external_id=platform_user_id,
    )
    return None if principal is None else principal.account_id


async def load_purge_preview(
    sm: async_sessionmaker[AsyncSession],
    *,
    account_id: uuid.UUID,
) -> PurgePreview:
    """Thin wrapper around daimon.core.privacy.collect_purge_preview.

    Takes a session-maker (not a session) so `collect_purge_preview` can open
    its own transaction internally.
    """
    return await collect_purge_preview(sm=sm, account_id=account_id)
