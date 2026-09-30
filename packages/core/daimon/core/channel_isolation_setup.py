"""Turning channel isolation on and off, shared by the tool, the panels and the CLI.

Isolating channel C needs C to have its own agent: one set as C's default
that answers nowhere else and is not built in. Without one the request is
refused with a reason, unless the caller passes a `fork` callable: then the
`fork_from` agent (default: whoever answers in C now) is copied into a new
agent named after the channel and set as C's default in the same
transaction that isolates C. Each adapter injects its own fork, so core
never imports one.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Literal

from anthropic import AsyncAnthropic
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_isolation import BindingRefusal, build_channel_isolation
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.errors import DaimonError
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, pick_agent
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    set_access_policy,
)
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import list_handoff_parent_channel_ids
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ForkAgent = Callable[[str, str], Awaitable[None]]
"""Copy the agent named first into a new agent named second."""

IsolationRefusal = Literal["no_channel_agent", "shared_channel_agent", "managed_channel_agent"]


class ChannelIsolationRefused(DaimonError):
    """Isolation was refused; the message is person-facing and names no ids."""

    def __init__(
        self, reason: IsolationRefusal | BindingRefusal, *, agent_name: str | None
    ) -> None:
        super().__init__(render_isolation_refusal(reason, agent_name=agent_name))
        self.reason: IsolationRefusal | BindingRefusal = reason


def render_isolation_refusal(
    reason: IsolationRefusal | BindingRefusal, *, agent_name: str | None
) -> str:
    name = agent_name or "the agent answering here"
    match reason:
        case "no_channel_agent":
            return (
                "This channel has no agent of its own, so it can't be isolated yet. "
                "Isolate it with a copy of the agent answering here instead."
            )
        case "shared_channel_agent":
            return (
                f"{name} also answers outside this channel, so it can't belong to this one. "
                f"Isolate the channel with a copy of {name} instead."
            )
        case "managed_channel_agent":
            return (
                f"{name} is built in, so it can't belong to one channel. "
                f"Isolate the channel with a copy of {name} instead."
            )
        case "channel_needs_own_agent":
            return (
                f"This channel is isolated, so only its own agents answer here, and {name} "
                f"answers elsewhere or is built in. Use a copy of {name} instead."
            )
        case "agent_confined":
            return f"{name} belongs to another isolated channel, so it can't be used here."
        case "channel_isolated":
            return (
                "This channel is isolated, so it keeps an agent of its own. Set another agent "
                "of its own, or end its isolation first."
            )


@dataclass(frozen=True)
class IsolationChange:
    """What `set_channel_isolation` did."""

    channel_id: str
    isolated: bool
    agent_name: str | None
    """The channel's own agent after the change; None when isolation was ended."""
    forked_from: str | None
    changed: bool


def isolated_agent_name(
    channel_label: str | None, channel_id: str, *, taken: Collection[str]
) -> str:
    """A new agent name from the channel's name, unique among `taken`. Pure."""
    slug = re.sub(r"[^a-z0-9]+", "-", (channel_label or "").lower()).strip("-")[:40].strip("-")
    base = slug or f"channel-{channel_id[-6:].lower()}"
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}-{suffix}", suffix + 1
    return name


async def _write_policy(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str, isolated: bool
) -> bool:
    await lock_access_policy(session, tenant_id=tenant_id)
    current = await load_access_policy(session, tenant_id=tenant_id)
    ids = [value for value in current.isolated_channel_ids if value != channel_id]
    if isolated:
        ids.append(channel_id)
    if tuple(ids) == current.isolated_channel_ids:
        return False
    updated = TenantAccessPolicy.model_validate(
        current.model_dump() | {"isolated_channel_ids": tuple(ids)}
    )
    await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    return True


async def set_channel_isolation(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    isolated: bool,
    default: DeploymentDefault,
    actor_account_id: uuid.UUID | None,
    channel_label: str | None = None,
    fork: ForkAgent | None = None,
    fork_from: str | None = None,
) -> IsolationChange:
    """Isolate `channel_id` or end its isolation; raise `ChannelIsolationRefused` if refused.

    A fork only happens when the channel has no usable agent of its own, so
    repeating a request with `fork` never copies twice.
    """
    if not isolated:
        async with sessionmaker.begin() as session:
            changed = await _write_policy(
                session, tenant_id=tenant_id, channel_id=channel_id, isolated=False
            )
        return IsolationChange(channel_id, False, None, None, changed)
    async with sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
        tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
        parents = await list_handoff_parent_channel_ids(session, tenant_id=tenant_id)
    row = next((row for row in channels if row.channel_id == channel_id), None)
    channel_agent = row.agent_name if row is not None and row.mode == "agent" else None
    isolation = build_channel_isolation(
        {*policy.isolated_channel_ids, channel_id},
        tenant=tenant,
        channels=channels,
        default=default,
        thread_parent_channel_ids=parents,
    )
    refusal: IsolationRefusal | None = "no_channel_agent"
    if channel_agent is not None:
        agent = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=channel_agent)
        if agent is None:
            refusal = "no_channel_agent"
        elif agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
            refusal = "managed_channel_agent"
        elif isolation.channel_of(channel_agent) != channel_id:
            refusal = "shared_channel_agent"
        else:
            refusal = None
    if refusal is None:
        async with sessionmaker.begin() as session:
            changed = await _write_policy(
                session, tenant_id=tenant_id, channel_id=channel_id, isolated=True
            )
        return IsolationChange(channel_id, True, channel_agent, None, changed)
    source = fork_from or pick_agent(row, tenant, default)[0]
    if fork is None or source is None:
        raise ChannelIsolationRefused(refusal, agent_name=channel_agent or source)
    owner = isolation.channel_of(source)
    if owner is not None and owner != channel_id:
        raise ChannelIsolationRefused("agent_confined", agent_name=source)
    taken = {
        agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name
        for agent in await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    }
    new_name = isolated_agent_name(channel_label, channel_id, taken=taken)
    await fork(source, new_name)
    async with sessionmaker.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
            tenant_id=tenant_id,
            agent_name=new_name,
            mode="agent",
            actor_account_id=actor_account_id,
        )
        await _write_policy(session, tenant_id=tenant_id, channel_id=channel_id, isolated=True)
    return IsolationChange(channel_id, True, new_name, source, True)


__all__ = [
    "ChannelIsolationRefused",
    "ForkAgent",
    "IsolationChange",
    "IsolationRefusal",
    "isolated_agent_name",
    "render_isolation_refusal",
    "set_channel_isolation",
]
