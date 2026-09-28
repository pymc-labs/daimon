"""May the agent post anything at all where this turn would answer?

`admit()` refuses a turn whose reply would land in a write-protected channel,
but adapters post notices of their own around admission: setup, capacity,
role lookup, refusals, rendered errors. So each turn entry computes a
`ProtectionState` FIRST -- before tenant liveness, provisioning or any other
read that can fail -- and every adapter post in that turn's prologue, denial
and error paths goes through one may-post check: only `UNPROTECTED` posts.
`PROTECTED` and `UNKNOWN` log instead.

Computing the state never raises. Any failure -- a policy that doesn't parse,
the database or pool, the category lookup's platform call -- yields `UNKNOWN`,
because a channel whose safety can't be established gets nothing, not even an
error.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable

import structlog
from daimon.core.access_policy import is_write_protected
from daimon.core.stores.access_policy import load_access_policy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

# Looks up the Discord category of the turn's channel: (category id, unresolved).
CategoryLookup = Callable[[], Awaitable[tuple[str | None, bool]]]


class ProtectionState(enum.StrEnum):
    UNPROTECTED = "unprotected"
    PROTECTED = "protected"
    UNKNOWN = "unknown"

    @property
    def may_post(self) -> bool:
        return self is ProtectionState.UNPROTECTED


async def protection_state(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    thread_id: str | None = None,
    resolve_category: CategoryLookup | None = None,
) -> ProtectionState:
    """Whether the agent may post for a turn in this channel or thread.

    The channel and its parent are checked first; ``resolve_category`` (which
    may cost a platform call) runs only when they aren't protected and the
    policy protects a category. A category that can't be resolved then counts
    as protected. Never raises: any failure is `UNKNOWN`.
    """
    try:
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        target = thread_id or channel_id
        if is_write_protected(policy, channel_id=target, parent_channel_id=channel_id):
            return ProtectionState.PROTECTED
        if resolve_category is None or not policy.protected_category_ids:
            return ProtectionState.UNPROTECTED
        category_id, category_unresolved = await resolve_category()
        if is_write_protected(
            policy,
            channel_id=target,
            parent_channel_id=channel_id,
            category_id=category_id,
            category_unresolved=category_unresolved,
        ):
            return ProtectionState.PROTECTED
        return ProtectionState.UNPROTECTED
    except Exception as exc:  # the state must always be decided; failure = unknown
        log.warning(
            "access_policy.protection_unknown",
            tenant_id=str(tenant_id),
            channel_id=channel_id,
            error=type(exc).__name__,
        )
        return ProtectionState.UNKNOWN


async def turn_target_protected(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    thread_id: str | None = None,
    resolve_category: CategoryLookup | None = None,
) -> bool:
    """True unless `protection_state` says the agent may post here."""
    state = await protection_state(
        sessionmaker,
        tenant_id=tenant_id,
        channel_id=channel_id,
        thread_id=thread_id,
        resolve_category=resolve_category,
    )
    return not state.may_post
