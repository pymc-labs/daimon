"""Who asked never to be tagged: the lookup every mention-rendering call makes."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def tag_opt_outs(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_ids: Collection[str],
) -> frozenset[str]:
    """The people among ``user_ids`` who opted out of tags.

    Nobody can opt out yet, so this is empty; the signature is the seam the
    person-scoped store plugs into.
    """
    del sessionmaker, tenant_id, platform, user_ids
    return frozenset()
