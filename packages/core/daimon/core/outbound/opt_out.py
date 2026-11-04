"""Who asked never to be tagged: the lookup every mention-rendering call makes.

A person opts out once, by account, and it holds on every platform identity
linked to that account. Mentions of them become plain ``@Name``; nothing
Daimon posts pings them.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from daimon.core.outbound.mentions import Platform, mentioned_user_ids, untag
from daimon.core.stores import platform_names as names_store
from daimon.core.stores import tag_opt_outs as store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def tag_opt_outs(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_ids: Collection[str],
) -> frozenset[str]:
    """The people among ``user_ids`` who opted out of tags."""
    if sessionmaker is None or not user_ids:
        return frozenset()
    async with sessionmaker() as session:
        return await store.opted_out_user_ids(
            session, tenant_id=tenant_id, platform=platform, user_ids=user_ids
        )


async def opt_out_names(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_ids: Collection[str],
) -> dict[str, str]:
    """The stored display name of each of ``user_ids`` that has one."""
    if sessionmaker is None or not user_ids:
        return {}
    async with sessionmaker() as session:
        known = await names_store.get_user_names(
            session, tenant_id=tenant_id, platform=platform, user_ids=user_ids
        )
    return {uid: name.label for uid, name in known.items() if name.label}


async def untag_opted_out(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    *,
    tenant_id: uuid.UUID | None,
    platform: Platform,
    text: str,
) -> str:
    """``text`` with the mention of everyone who opted out turned into a plain name.

    For text an adapter posts itself (replies, notices, routine results). A
    Teams body carries no mention markup of its own, so it passes untouched.
    """
    if tenant_id is None or sessionmaker is None or platform == "teams":
        return text
    mentioned = mentioned_user_ids(text, platform)
    skip = await tag_opt_outs(
        sessionmaker, tenant_id=tenant_id, platform=platform, user_ids=mentioned
    )
    if not skip:
        return text
    names = await opt_out_names(sessionmaker, tenant_id=tenant_id, platform=platform, user_ids=skip)
    return untag(text, platform, skip, names)
