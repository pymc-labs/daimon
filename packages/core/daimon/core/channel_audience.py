"""Who can read a channel: the one question every hold reader asks.

`channel_has_outsiders` answers whether people outside the team may read a
channel. Slack and Teams use the sticky registry of shared channels (`shared_channels`):
a channel recorded as shared stays shared, and nothing here writes to the registry.
A Discord server in mode `on` uses the stored room facts instead
(`stores.audience_facts`), so a room opens again when its outsider leaves. Mode `shadow`
and mode `off` answer from the registry as before; `shadow` also carries the facts answer
in `ChannelAudience.shadow` so a caller can log what `on` would do.
`audience_mode` reads the server's `audience_hold` setting.
`requester_standing` says whether a Discord requester is HOST or OUTSIDER.

A reader compares the mode by `.value`, never by `.on`: `shadow` counts as on there,
and the mode `shadow` must change nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

import structlog
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.membership import MembershipResolver
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.audience_facts import read_channel_facts
from daimon.core.stores.channel_directory import list_shared_channel_ids
from daimon.core.stores.feature_values import MODE_READS_KEY
from daimon.core.stores.shared_channels import is_channel_shared
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.workspace_members import member_is_internal
from daimon.core.turn_hold import RequesterOrg
from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)

AudienceMode = Literal["off", "shadow", "on"]
AudienceSource = Literal["registry", "facts", "none"]
ShadowKind = Literal[
    "turn_held",
    "tool_removed",
    "outsider",
    "dm_refused",
    "private_offer",
    "memory_read_only",
]


@dataclass(frozen=True)
class ChannelAudience:
    """What is known about who reads a channel.

    ``contained``: outsiders may read it, or nobody can say. ``known``: the answer
    comes from a recorded fact. ``narrow``: the fail-closed value for a channel the
    answer is unknown for. ``source`` names where the answer came from.
    """

    contained: bool
    known: bool
    narrow: bool
    source: AudienceSource
    #: In mode `shadow` on Discord: what the stored facts say. Nothing enforces it.
    shadow: ChannelAudience | None = None


async def channel_has_outsiders(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None = None,
    mode: AudienceMode | None = None,
    now: datetime | None = None,
) -> ChannelAudience:
    """The audience of a channel (`channel_id` is the parent channel; `thread_id` a thread in it).

    Slack, Teams, and Discord in mode `off` or `shadow`: the sticky registry. A recorded
    channel is contained; one with no record is open, which is what `is_channel_shared`
    says. Discord in mode `on`: the stored room facts. An unknown room is contained and
    narrow. `mode` is the server's mode when the caller already read it; None reads it
    here (Discord only).
    """
    if platform == "discord":
        if mode is None:
            mode = await audience_mode(session, FeaturePlace(tenant_id, platform, channel_id))
        if mode != "off":
            live = await _facts_audience(
                session,
                tenant_id=tenant_id,
                channel_id=channel_id,
                thread_id=thread_id,
                now=now,
            )
            if mode == "on":
                return live
            return _registry_audience(
                await is_channel_shared(
                    session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
                ),
                shadow=live,
            )
    return _registry_audience(
        await is_channel_shared(
            session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
        )
    )


def _registry_audience(shared: bool, *, shadow: ChannelAudience | None = None) -> ChannelAudience:
    if shared:
        return ChannelAudience(
            contained=True, known=True, narrow=False, source="registry", shadow=shadow
        )
    return ChannelAudience(contained=False, known=False, narrow=False, source="none", shadow=shadow)


async def _facts_audience(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    thread_id: str | None,
    now: datetime | None,
) -> ChannelAudience:
    """The stored facts of a room, a thread falling back to its parent channel."""
    room = thread_id if thread_id is not None and thread_id != channel_id else channel_id
    facts = await read_channel_facts(
        session,
        tenant_id=tenant_id,
        channel_id=room,
        now=now or datetime.now(UTC),
        parent_id=channel_id if room != channel_id else None,
    )
    return ChannelAudience(
        contained=facts.contained, known=facts.known, narrow=facts.narrow, source="facts"
    )


async def channels_with_outsiders(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str
) -> set[str]:
    """The channels `channel_has_outsiders` calls contained, in one read, for a channel list."""
    return await list_shared_channel_ids(session, tenant_id=tenant_id, platform=platform)


def mode_of(value: str) -> AudienceMode:
    """The `audience_hold` mode a resolved feature value means; anything else is `off`."""
    if value == "on":
        return "on"
    if value == "shadow":
        return "shadow"
    return "off"


async def audience_mode(
    session: AsyncSession, place: FeaturePlace, *, settings: Settings | None = None
) -> AudienceMode:
    """The server's `audience_hold` mode. A failed read raises: it never falls back to a mode.

    A transaction reads each server once: the policy load, the hold and the write guard share
    the answer. A new transaction, or a feature write in this one, reads it again."""
    # The setting has only deployment and workspace tiers: every room of a server shares it.
    key = (place.tenant_id, place.platform, None if settings is None else id(settings))
    held = cast(
        "tuple[object, dict[object, AudienceMode]] | None", session.info.get(MODE_READS_KEY)
    )
    transaction = session.get_transaction()
    if transaction is not None and held is not None and held[0] is transaction and key in held[1]:
        return held[1][key]
    resolved = await feature_in(session, settings, place, "audience_hold")
    mode = mode_of(resolved.value)
    transaction = session.get_transaction()  # the read itself may have begun it
    if transaction is not None:
        if held is None or held[0] is not transaction:
            held = (transaction, dict[object, AudienceMode]())
            session.info[MODE_READS_KEY] = held
        held[1][key] = mode
    return mode


def log_shadow(
    kind: ShadowKind,
    *,
    tenant_id: uuid.UUID,
    channel_id: str | None,
    agent: str | None,
    reason: str,
) -> None:
    """One `audience.shadow` event: what mode `on` would do. Ids and a reason only."""
    _log.info(
        "audience.shadow",
        kind=kind,
        tenant_id=str(tenant_id),
        channel_id=channel_id,
        agent=agent,
        reason=reason,
    )


async def requester_standing(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    channel_id: str | None = None,
    is_dm: bool = False,
    event_role_ids: Collection[str] | None = None,
    resolver: MembershipResolver | None = None,
    mode: AudienceMode | None = None,
) -> RequesterOrg:
    """HOST or OUTSIDER for a Discord requester: whether they hold an internal role.

    Sources in order: the roles in the event, the member mirror, one fresh `resolver`
    lookup (a member fetch), then OUTSIDER for this turn only. Nothing is recorded. A server with no
    `internal_role_ids`, and mode `off`, give HOST. Mode `shadow` gives HOST and logs
    the person it would refuse (`outsider`, or `dm_refused` for a private conversation).
    """
    if mode is None:
        mode = await audience_mode(session, FeaturePlace(tenant_id, "discord", channel_id))
    if mode == "off":
        return RequesterOrg.HOST
    internal_roles = (await load_access_policy(session, tenant_id=tenant_id)).internal_role_ids
    if not internal_roles:
        return RequesterOrg.HOST
    internal = await _is_internal(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        internal_roles=internal_roles,
        event_role_ids=event_role_ids,
        resolver=resolver,
    )
    if internal:
        return RequesterOrg.HOST
    if mode == "on":
        return RequesterOrg.OUTSIDER
    log_shadow(
        "dm_refused" if is_dm else "outsider",
        tenant_id=tenant_id,
        channel_id=channel_id,
        agent=None,
        reason="no_internal_role",
    )
    return RequesterOrg.HOST


async def _is_internal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    internal_roles: Collection[str],
    event_role_ids: Collection[str] | None,
    resolver: MembershipResolver | None,
) -> bool:
    if event_role_ids is not None:
        return bool(set(event_role_ids) & set(internal_roles))
    mirrored = await member_is_internal(
        session, tenant_id=tenant_id, user_id=user_id, internal_role_ids=internal_roles
    )
    if mirrored is not None:
        return mirrored
    if resolver is None:
        return False
    try:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None:
            return False
        found = await resolver.resolve(tenant_id, tenant.external_id, user_id, require_fresh=True)
    except Exception:
        _log.warning("audience.standing_fetch_failed", tenant_id=str(tenant_id), exc_info=True)
        return False
    return found.is_member is True and bool(set(found.role_ids) & set(internal_roles))


__all__ = [
    "AudienceMode",
    "ChannelAudience",
    "ShadowKind",
    "audience_mode",
    "mode_of",
    "channel_has_outsiders",
    "channels_with_outsiders",
    "log_shadow",
    "requester_standing",
]
