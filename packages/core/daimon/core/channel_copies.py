"""Archive the copy a channel kept to its own agents was given, once that channel closes.

`set_channel_rule` stamps each copy it makes with its channel
(`MA_METADATA_KEY_CHANNEL_COPY`). `archive_channel_copy` archives only
such a copy, never a built-in agent or a workspace or deployment default.
A copy with an agent rule or a channel's default is refused, unless that one
place is the channel named as closing, its own: its rule and default there go
with it. The channel keeps its rule, so nothing answers there after.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import agent_pin_names
from daimon.core.authz import Action, Subject, authorize
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_CHANNEL_COPY, MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.memory_resource import archive_memory_store_for_agent
from daimon.core.permissions import AgentRule, with_agent_rule
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.scoped_config_write import clear_agent_references
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

ArchiveRefusal = Literal[
    "admin_required",
    "not_found",
    "ambiguous",
    "not_channel_copy",
    "other_channel",
    "default_agent",
    "has_rule",
    "channel_default",
]

_REFUSALS: dict[ArchiveRefusal, str] = {
    "admin_required": "Only a workspace or server admin can archive an agent.",
    "not_found": "There is no agent by that name.",
    "ambiguous": "More than one agent has that name, or it changed: list the agents again.",
    "not_channel_copy": (
        "That agent wasn't made as a channel's own copy, so this tool doesn't archive it."
    ),
    "other_channel": "That agent was made for another channel.",
    "default_agent": "That agent is a workspace or deployment default.",
    "has_rule": (
        "That agent still has a rule naming a channel. Name the channel being closed, or "
        "drop its rule first."
    ),
    "channel_default": (
        "That agent is still a channel's default. Name the channel being closed, or change "
        "the default first."
    ),
}


class ChannelCopyArchiveRefused(DaimonError):
    """The archive was refused; the message is person-facing."""

    def __init__(self, reason: ArchiveRefusal) -> None:
        super().__init__(_REFUSALS[reason])
        self.reason: ArchiveRefusal = reason


@dataclass(frozen=True)
class ArchivedCopy:
    name: str
    agent_id: str
    closed_channel_id: str | None


def archive_refusal(
    *,
    name: str,
    metadata: Mapping[str, str],
    policy: TenantAccessPolicy,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    closing_channel_id: str | None,
) -> ArchiveRefusal | None:
    """Why the copy can't be archived now; None when it can. Pure."""
    made_for = metadata.get(MA_METADATA_KEY_CHANNEL_COPY)
    if not made_for or metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        return "not_channel_copy"
    if closing_channel_id is not None and closing_channel_id != made_for:
        return "other_channel"
    names = {n for n in agent_pin_names(name, metadata) if n}
    if names & {tenant.agent_name if tenant is not None else None, default.agent_name}:
        return "default_agent"
    closing: set[str] = {closing_channel_id} if closing_channel_id is not None else set()
    for each in names:
        rule = policy.agent_rules.get(each)
        runs_in = rule.runs_in if rule is not None else None
        if runs_in is not None and (not closing or set(runs_in) - closing):
            return "has_rule"
    if any(row.agent_name in names and row.channel_id not in closing for row in channels):
        return "channel_default"
    return None


def _without_rules(policy: TenantAccessPolicy, names: set[str]) -> TenantAccessPolicy:
    for name in names:
        policy = with_agent_rule(policy, name, AgentRule())
    return policy


async def _find(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, name: str, expected_ma_agent_id: str | None
) -> BetaManagedAgentsAgent:
    found = await find_agents_by_daimon_tag(client, tenant_id=tenant_id, name=name)
    if not found:
        raise ChannelCopyArchiveRefused("not_found")
    if len(found) > 1 or (expected_ma_agent_id not in (None, found[0].id)):
        raise ChannelCopyArchiveRefused("ambiguous")
    return found[0]


async def archive_channel_copy(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    closing_channel_id: str | None,
    subject: Subject,
    default: DeploymentDefault,
    expected_ma_agent_id: str | None = None,
) -> ArchivedCopy:
    """Archive `name`; raise `ChannelCopyArchiveRefused` unless it may go.

    Decided and written under the policy lock, archiving first: a failed
    write then leaves only a rule or default naming an archived agent, never
    a copy freed from its channel and still running.
    """
    agent = await _find(
        client, tenant_id=tenant_id, name=name, expected_ma_agent_id=expected_ma_agent_id
    )
    names = {n for n in agent_pin_names(agent.name, agent.metadata) if n}
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        await lock_access_policy(session, tenant_id=tenant_id)
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if not authorize(policy, subject=subject, action=Action.ARCHIVE_CHANNEL_COPY):
            raise ChannelCopyArchiveRefused("admin_required")
        tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
        refusal = archive_refusal(
            name=name,
            metadata=agent.metadata,
            policy=policy,
            tenant=tenant,
            channels=channels,
            default=default,
            closing_channel_id=closing_channel_id,
        )
        if refusal is not None:
            raise ChannelCopyArchiveRefused(refusal)
        await client.beta.agents.archive(agent.id)
        updated = _without_rules(policy, names)
        if updated != policy:
            await set_access_policy(session, tenant_id=tenant_id, policy=updated)
        for each in names:
            await clear_agent_references(session, tenant_id=tenant_id, agent_name=each)
    try:
        await archive_memory_store_for_agent(
            client,
            sessionmaker,
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id),
        )
    except anthropic.APIError:
        # The agent is archived already; its store is an audit trail, kept.
        _log.warning(
            "channel_copy.memory_store_archive_failed", tenant_id=str(tenant_id), agent=name
        )
    return ArchivedCopy(name=name, agent_id=agent.id, closed_channel_id=closing_channel_id)
