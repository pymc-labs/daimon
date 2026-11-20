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
from dataclasses import dataclass
from typing import Literal

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_gate import PostAudience, audience_fact
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.channel_audience import ChannelAudience
from daimon.core.permissions import any_writers_none, channel_rule
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.turn_hold import TurnHold
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
    audience: PostAudience | None = None,
) -> ProtectionState:
    """Whether the agent may post for a turn in this channel or thread.

    The channel and its parent are checked first; ``resolve_category`` (which
    may cost a platform call) runs only when they aren't protected and the
    policy protects a category. A category that can't be resolved then counts
    as protected. Never raises: any failure is `UNKNOWN`.

    ``audience`` opts the call into the audience gate (`daimon.core.audience_gate`):
    a post that is more than a fixed notice in the turn's own room passes the hold the
    execution runs under, and a contained destination it is not held to is
    `PROTECTED`. Without it nothing about the audience is read.
    """
    try:
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        target = thread_id or channel_id
        fact = (
            await audience_fact(
                sessionmaker,
                tenant_id=tenant_id,
                platform=audience.platform,
                channel_id=target,
                parent_id=channel_id if thread_id else None,
                hold=audience.hold,
                agent=audience.agent,
                held_if_contained=audience.held_if_contained,
            )
            if audience is not None
            else None
        )
        hold = audience.hold_for(fact, channel_id) if audience and fact else None
        if not _may_post(
            policy, Place(channel_id=target, parent_channel_id=channel_id), fact, hold
        ):
            return ProtectionState.PROTECTED
        if resolve_category is None or not any_writers_none(policy, categories_only=True):
            return ProtectionState.UNPROTECTED
        category_id, category_unresolved = await resolve_category()
        if not _may_post(
            policy,
            Place(
                channel_id=target,
                parent_channel_id=channel_id,
                category_id=category_id,
                category_unresolved=category_unresolved,
            ),
            fact,
            hold,
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


def _may_post(
    policy: TenantAccessPolicy,
    place: Place,
    audience: ChannelAudience | None = None,
    hold: TurnHold | None = None,
) -> bool:
    """Protection alone: an adapter's own notice, decided before the agent is known."""
    return bool(
        authorize(
            policy,
            subject=Subject(),
            action=Action.POST,
            place=place,
            # The hold counts only while the audience is enforced (mode `on`).
            hold=hold if audience is not None else None,
            audience=audience,
        )
    )


async def turn_target_protected(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    thread_id: str | None = None,
    resolve_category: CategoryLookup | None = None,
    audience: PostAudience | None = None,
) -> bool:
    """True unless `protection_state` says the agent may post here."""
    state = await protection_state(
        sessionmaker,
        tenant_id=tenant_id,
        channel_id=channel_id,
        thread_id=thread_id,
        resolve_category=resolve_category,
        audience=audience,
    )
    return not state.may_post


@dataclass(frozen=True, slots=True)
class ConfirmedProtection:
    """A Who-can-post-Nobody rule that positively matches a place.

    ``scope`` says where the rule sits and ``rule_id`` is that channel or
    category id. Unlike `ProtectionState.PROTECTED`, this is never a fail-closed
    guess, so it is safe to tell a user "this place's rule is Nobody".
    """

    scope: Literal["channel", "category"]
    rule_id: str


def matched_writers_none(
    policy: TenantAccessPolicy,
    *,
    channel_id: str,
    thread_id: str | None = None,
    category_id: str | None = None,
) -> ConfirmedProtection | None:
    """The writers-none rule that applies here, only if one positively matches.

    ``channel_id`` is the channel (a thread's parent); ``category_id`` must be
    a resolved category or None. An unresolved category matches nothing here,
    even though the turn gate counts it as protected.
    """
    for candidate in (thread_id, channel_id):
        if candidate is not None and channel_rule(policy, candidate).writers == "none":
            return ConfirmedProtection(scope="channel", rule_id=candidate)
    rule = policy.category_rules.get(category_id) if category_id is not None else None
    if rule is not None and rule.writers == "none":
        return ConfirmedProtection(scope="category", rule_id=category_id or "")
    return None


async def confirmed_protection(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    thread_id: str | None = None,
    category_id: str | None = None,
) -> ConfirmedProtection | None:
    """`matched_writers_none` over the tenant's stored policy. Never raises:
    a failed read confirms nothing."""
    try:
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=tenant_id)
    except Exception as exc:
        log.warning(
            "access_policy.confirm_protection_failed",
            tenant_id=str(tenant_id),
            channel_id=channel_id,
            error=type(exc).__name__,
        )
        return None
    return matched_writers_none(
        policy, channel_id=channel_id, thread_id=thread_id, category_id=category_id
    )
