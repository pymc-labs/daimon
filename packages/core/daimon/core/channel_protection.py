"""Protect or seal one channel, or lift either, in one locked policy write.

`set_channel_protection` asks `authorize(SET_CHANNEL_PROTECTION)` against the
policy it locks, so a server admin may change any channel and a channel admin
their own, short of lifting a seal. An isolated channel keeps its seal until
its isolation ends.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from anthropic import AsyncAnthropic
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.channel_environments import sealed_network_warning
from daimon.core.errors import DaimonError
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    set_access_policy,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ProtectionRefusal = Literal["admin_required", "seal_admin_required", "isolated"]

_REFUSALS: dict[str, str] = {
    "admin_required": "This change needs a server admin, or an admin of this channel.",
    "seal_admin_required": "Lifting a channel's seal needs a server admin.",
    "isolated": "This channel is isolated, so it stays sealed: end its isolation first.",
}


class ChannelProtectionRefused(DaimonError):
    """The change was refused; the message is person-facing."""

    def __init__(self, reason: ProtectionRefusal) -> None:
        super().__init__(_REFUSALS[reason])
        self.reason: ProtectionRefusal = reason


@dataclass(frozen=True)
class ProtectionChange:
    channel_id: str
    protected: bool
    sealed: bool
    changed: bool
    network_warning: str | None = None
    """Set when the change sealed a channel whose own environment has an open network."""


def _toggled(ids: tuple[str, ...], channel_id: str, on: bool | None) -> tuple[str, ...]:
    if on is None or (channel_id in ids) == on:
        return ids
    return (*ids, channel_id) if on else tuple(i for i in ids if i != channel_id)


def toggle_channel(
    policy: TenantAccessPolicy, *, channel_id: str, protected: bool | None, sealed: bool | None
) -> TenantAccessPolicy:
    """`policy` with `channel_id` protected and sealed as asked; None keeps that one.

    Raise `ChannelProtectionRefused("isolated")` for unsealing an isolated channel.
    """
    if sealed is False and channel_id in policy.isolated_channel_ids:
        raise ChannelProtectionRefused("isolated")
    return TenantAccessPolicy.model_validate(
        policy.model_dump()
        | {
            "protected_channel_ids": _toggled(policy.protected_channel_ids, channel_id, protected),
            "sealed_channel_ids": _toggled(policy.sealed_channel_ids, channel_id, sealed),
        }
    )


async def set_channel_protection(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    protected: bool | None,
    sealed: bool | None,
    subject: Subject,
    default: DeploymentDefault,
) -> ProtectionChange:
    """Apply the toggle; raise `ChannelProtectionRefused` when it isn't the subject's to make."""
    async with sessionmaker.begin() as session:
        await lock_access_policy(session, tenant_id=tenant_id)
        policy = await load_access_policy(session, tenant_id=tenant_id)
        lifts_seal = sealed is False and channel_id in policy.sealed_channel_ids

        def decide(*, lifts: bool) -> bool:
            return bool(
                authorize(
                    policy,
                    subject=subject,
                    action=Action.SET_CHANNEL_PROTECTION,
                    place=Place(channel_id=channel_id),
                    lifts_seal=lifts,
                )
            )

        if not decide(lifts=lifts_seal):
            seal_only = lifts_seal and decide(lifts=False)
            raise ChannelProtectionRefused("seal_admin_required" if seal_only else "admin_required")
        updated = toggle_channel(policy, channel_id=channel_id, protected=protected, sealed=sealed)
        if updated != policy:
            await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    newly_sealed = channel_id in updated.sealed_channel_ids and (
        channel_id not in policy.sealed_channel_ids
    )
    warning = None
    if newly_sealed:
        async with sessionmaker() as session:
            warning = await sealed_network_warning(
                session, anthropic, tenant_id=tenant_id, channel_id=channel_id, default=default
            )
    return ProtectionChange(
        channel_id=channel_id,
        protected=channel_id in updated.protected_channel_ids,
        sealed=channel_id in updated.sealed_channel_ids,
        changed=updated != policy,
        network_warning=warning,
    )
