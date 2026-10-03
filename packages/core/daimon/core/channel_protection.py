"""Protect or seal one channel, or lift either, in one locked policy write.

`set_channel_protection` asks `authorize(SET_CHANNEL_PROTECTION)` against the
policy it locks: server admins and operator tokens only, never a channel
admin. An isolated channel keeps its seal until its isolation ends.
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
from daimon.core.permissions import ChannelRule, channel_rule, with_channel_rule
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ProtectionRefusal = Literal["admin_required", "isolated"]

_REFUSALS: dict[ProtectionRefusal, str] = {
    "admin_required": "Protecting or sealing a channel needs a server admin.",
    "isolated": "This channel is confidential, so it stays sealed: unmark it confidential first.",
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


def toggle_channel(
    policy: TenantAccessPolicy, *, channel_id: str, protected: bool | None, sealed: bool | None
) -> TenantAccessPolicy:
    """`policy` with `channel_id` protected and sealed as asked; None keeps that one.

    Protecting sets the channel rule's writers to none, sealing its readers to
    inside (`daimon.core.permissions`). Raise `ChannelProtectionRefused("isolated")`
    for unsealing an isolated (confidential) channel.
    """
    current = channel_rule(policy, channel_id)
    if current.readers == "own":
        if sealed is False:
            raise ChannelProtectionRefused("isolated")
        readers = current.readers
    else:
        readers = current.readers if sealed is None else "inside" if sealed else "any"
    unprotected = "own" if readers == "own" else "any"
    writers = current.writers if protected is None else "none" if protected else unprotected
    return with_channel_rule(policy, channel_id, ChannelRule(readers=readers, writers=writers))


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
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        await lock_access_policy(session, tenant_id=tenant_id)
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if not authorize(
            policy,
            subject=subject,
            action=Action.SET_CHANNEL_PROTECTION,
            place=Place(channel_id=channel_id),
        ):
            raise ChannelProtectionRefused("admin_required")
        updated = toggle_channel(policy, channel_id=channel_id, protected=protected, sealed=sealed)
        if updated != policy:
            await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    before, after = channel_rule(policy, channel_id), channel_rule(updated, channel_id)
    newly_sealed = after.readers != "any" and before.readers == "any"
    warning = None
    if newly_sealed:
        async with sessionmaker() as session:
            warning = await sealed_network_warning(
                session, anthropic, tenant_id=tenant_id, channel_id=channel_id, default=default
            )
    return ProtectionChange(
        channel_id=channel_id,
        protected=after.writers == "none",
        sealed=after.readers != "any",
        changed=updated != policy,
        network_warning=warning,
    )
