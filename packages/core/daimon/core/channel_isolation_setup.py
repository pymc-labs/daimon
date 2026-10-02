"""Turning channel isolation on and off, shared by the tool, the panels and the CLI.

Isolating channel C seals it, pins its own agent to C alone and adds the
marker, in one write under the policy lock. The own agent is the agent set as
C's default, if it is not built in, pinned nowhere but C and answers nowhere
else (`daimon.core.agent_reach`, every name it carries). Otherwise the request
is refused with a reason, unless the caller asks for a copy: then the
`fork_from` agent (default: whoever answers in C now) is copied
(`daimon.core.agent_fork`) under a name taken from the channel, set as C's
default and pinned instead. Ending isolation drops the marker and leaves the
seal and the pins, unless asked to lift them too (`drop_seal_and_pins`): then
the channel is unsealed and every agent pinned to it alone is unpinned.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

import structlog
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import TenantAccessPolicy, isolation_owner
from daimon.core.agent_fork import fork_agent
from daimon.core.agent_pins import agent_pin_names
from daimon.core.agent_reach import load_agent_reach
from daimon.core.authz import Subject
from daimon.core.channel_environments import sealed_network_warning
from daimon.core.channel_isolation import BindingRefusal
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ISOLATION_COPY,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
)
from daimon.core.errors import DaimonError
from daimon.core.scope import ChannelConfigRow, ChannelScopeRef, DeploymentDefault, pick_agent
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    set_access_policy,
)
from daimon.core.stores.scoped_config_read import get_scope, list_propagations_for_tenant
from daimon.core.stores.scoped_config_write import set_fields
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

IsolationRefusal = Literal[
    "no_channel_agent",
    "shared_channel_agent",
    "pinned_shared_channel_agent",
    "managed_channel_agent",
    "pinned_elsewhere",
]

END_ISOLATION_WARNING = (
    "The channel stays private and its dedicated agents stay pinned to it, so they still "
    "answer only here, but they show elsewhere again and their memory turns read-only, as "
    "in any private channel. Lifting the seal and pins too lets them answer elsewhere, "
    "bringing what they remembered here."
)
LIFT_ISOLATION_WARNING = (
    "The channel is no longer private and its dedicated agents are no longer pinned, so "
    "they can answer elsewhere, bringing what they remembered here."
)


class ChannelIsolationRefused(DaimonError):
    """Isolation was refused; the message is person-facing."""

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
        case "pinned_shared_channel_agent":
            # A pinned agent can't be copied (`authorize(FORK)`), so no copy is offered.
            return (
                f"{name} also answers outside this channel, so it can't belong to this one, "
                "and a pinned agent can't be copied. Change its pin first."
            )
        case "pinned_elsewhere":
            return (
                f"{name} is pinned to other channels too, so it can't belong to this one, "
                "and a pinned agent can't be copied. Change its pin first."
            )
        case "channel_needs_own_agent":
            return (
                f"This channel is isolated, so only its own agents answer here, and {name} "
                f"is not one of them. Use a copy of {name} instead."
            )
        case "agent_confined":
            return f"{name} belongs to another isolated channel, so it can't be used here."
        case "agent_pinned":
            return (
                f"{name} is pinned to other channels, so it would refuse every turn here. "
                "Pick another agent, or change its pin first."
            )
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
    """The agent pinned to the channel by this change; None when isolation was ended."""
    forked_from: str | None
    changed: bool
    dropped_skills: tuple[str, ...] = ()
    """Skills left off the copy: another agent's, or the source's own that failed to copy."""
    lifted_seal_and_pins: bool = False
    """Isolation ended along with the channel's seal and its dedicated agents' pins."""
    network_warning: str | None = None
    """The channel's own environment has an open network nobody confirmed under the seal."""

    @property
    def end_warning(self) -> str:
        """What ending isolation leaves in place, for the person who ended it."""
        return LIFT_ISOLATION_WARNING if self.lifted_seal_and_pins else END_ISOLATION_WARNING

    @property
    def dropped_skills_note(self) -> str | None:
        if not self.dropped_skills:
            return None
        return f"Left off the copy: {', '.join(self.dropped_skills)}."


def isolated_agent_name(
    channel_label: str | None, channel_id: str, *, taken: Collection[str]
) -> str:
    """A new agent name from the channel's name, unique among `taken`. Pure."""
    slug = re.sub(r"[^a-z0-9]+", "-", (channel_label or "").lower()).strip("-")[:40].strip("-")
    # A Teams id (`19:<id>@thread.tacv2`) ends in its domain and holds ":", so
    # the tail comes from its id part, letters and digits only.
    tail = re.sub(r"[^a-z0-9]", "", channel_id.partition("@")[0].lower())[-6:]
    base = slug or f"channel-{tail}"
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}-{suffix}", suffix + 1
    return name


def isolate(policy: TenantAccessPolicy, *, channel_id: str, agent_name: str) -> TenantAccessPolicy:
    """`policy` with the channel sealed, `agent_name` pinned to it alone and marked. Pure."""
    data = policy.model_dump()
    data["sealed_channel_ids"] = tuple(dict.fromkeys((*policy.sealed_channel_ids, channel_id)))
    data["agent_channel_pins"] = {**policy.agent_channel_pins, agent_name: (channel_id,)}
    data["isolated_channel_ids"] = tuple(dict.fromkeys((*policy.isolated_channel_ids, channel_id)))
    return TenantAccessPolicy.model_validate(data)


def end_isolation(
    policy: TenantAccessPolicy, *, channel_id: str, drop_seal_and_pins: bool
) -> TenantAccessPolicy:
    """`policy` without the marker; with `drop_seal_and_pins`, without its seal and pins. Pure."""
    data = policy.model_dump()
    data["isolated_channel_ids"] = tuple(c for c in policy.isolated_channel_ids if c != channel_id)
    if drop_seal_and_pins:
        data["sealed_channel_ids"] = tuple(c for c in policy.sealed_channel_ids if c != channel_id)
        data["agent_channel_pins"] = {
            name: pin for name, pin in policy.agent_channel_pins.items() if set(pin) != {channel_id}
        }
    return TenantAccessPolicy.model_validate(data)


@dataclass(frozen=True)
class _ChannelAgent:
    name: str | None
    """The agent set as the channel's default, if any."""
    answering: str | None
    """The agent answering in the channel now, by the cascade."""
    refusal: IsolationRefusal | None
    names: tuple[str | None, ...] = ()
    """Every name the default agent carries, once found."""


async def _channel_agent(
    anthropic: AsyncAnthropic,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    policy: TenantAccessPolicy,
    default: DeploymentDefault,
    known: Mapping[str, BetaManagedAgentsAgent | None] | None = None,
) -> _ChannelAgent:
    """The channel's default agent and why it can't be the channel's own, if it can't.

    `known` holds agents already looked up by name (`_lookup_channel_agent`),
    so a caller holding the policy lock needn't wait on the network for them.
    """
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    row = next((row for row in channels if row.channel_id == channel_id), None)
    name = row.agent_name if row is not None and row.mode == "agent" else None
    answering = pick_agent(row, tenant, default)[0]
    if name is None:
        agent = None
    elif known is not None and name in known:
        agent = known[name]
    else:
        agent = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=name)
    if name is None or agent is None:
        return _ChannelAgent(name, answering, "no_channel_agent")
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        return _ChannelAgent(name, answering, "managed_channel_agent")
    names = (name, *agent_pin_names(agent.name, agent.metadata))
    pins = [policy.agent_channel_pins[n] for n in names if n and n in policy.agent_channel_pins]
    if any(set(pin) - {channel_id} for pin in pins):
        return _ChannelAgent(name, answering, "pinned_elsewhere")
    reach = await load_agent_reach(
        session,
        tenant_id=tenant_id,
        platform=platform,
        agent_names=tuple(n for n in names if n),
        ma_agent_id=agent.id,
        default=default,
    )
    if not reach.stays_inside({channel_id}):
        refusal: IsolationRefusal = (
            "pinned_shared_channel_agent" if pins else "shared_channel_agent"
        )
        return _ChannelAgent(name, answering, refusal, names)
    return _ChannelAgent(name, answering, None, names)


async def _lookup_channel_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
) -> dict[str, BetaManagedAgentsAgent | None]:
    """The channel's default agent looked up by name before the policy lock is taken.

    The lookup leaves the process; doing it first keeps a pooled connection
    from holding the lock while it waits. The locked read uses it only if the
    channel's agent is still the one looked up here.
    """
    async with sessionmaker() as session:
        _, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    row = next((row for row in channels if row.channel_id == channel_id), None)
    if row is None or row.mode != "agent" or row.agent_name is None:
        return {}
    return {
        row.agent_name: await find_agent_by_daimon_tag(
            anthropic, tenant_id=tenant_id, name=row.agent_name
        )
    }


async def isolation_refusal(
    anthropic: AsyncAnthropic,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    policy: TenantAccessPolicy,
    default: DeploymentDefault,
) -> ChannelIsolationRefused | None:
    """Why `policy` may not isolate `channel_id`; None when its default agent is its own.

    For a policy written whole (the CLI): the channel's default agent must
    already be pinned to it alone in `policy`, besides `set_channel_isolation`'s checks.
    """
    found = await _channel_agent(
        anthropic,
        session,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        policy=policy,
        default=default,
    )
    if found.refusal is not None:
        return ChannelIsolationRefused(found.refusal, agent_name=found.name)
    if isolation_owner(policy, found.names) != channel_id:
        return ChannelIsolationRefused("channel_needs_own_agent", agent_name=found.name)
    return None


async def _write_isolation(
    anthropic: AsyncAnthropic,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    default: DeploymentDefault,
    known: Mapping[str, BetaManagedAgentsAgent | None],
) -> tuple[_ChannelAgent, bool]:
    """Under the policy lock: isolate with the channel's default agent if it may be its own."""
    await lock_access_policy(session, tenant_id=tenant_id)
    policy = await load_access_policy(session, tenant_id=tenant_id)
    found = await _channel_agent(
        anthropic,
        session,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        policy=policy,
        default=default,
        known=known,
    )
    if found.refusal is not None or found.name is None:
        return found, False
    updated = isolate(policy, channel_id=channel_id, agent_name=found.name)
    if updated == policy:
        return found, False
    await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    return found, True


async def _network_warning(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    default: DeploymentDefault,
) -> str | None:
    async with sessionmaker() as session:
        return await sealed_network_warning(
            session, anthropic, tenant_id=tenant_id, channel_id=channel_id, default=default
        )


async def _archive_unused_copy(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    name: str,
    agent_id: str,
) -> None:
    """Archive a copy whose write failed, unless the channel may point at it.

    A commit can fail after the database applied it, so the channel is read
    again first; a copy it names, or one when that read fails, is kept and
    logged. A failed archive is logged too: the caller re-raises the write's
    own error.
    """
    log_fields = {"tenant_id": str(tenant_id), "channel_id": channel_id, "agent_id": agent_id}
    try:
        async with sessionmaker() as session:
            scope = await get_scope(
                session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id)
            )
    except (SQLAlchemyError, OSError):
        _log.warning(
            "channel_isolation.fork_kept", reason="unreadable", exc_info=True, **log_fields
        )
        return
    if isinstance(scope, ChannelConfigRow) and scope.agent_name == name:
        _log.warning("channel_isolation.fork_kept", reason="channel_points_at_it", **log_fields)
        return
    try:
        await anthropic.beta.agents.archive(agent_id)
    except APIError:
        _log.warning("channel_isolation.fork_archive_failed", exc_info=True, **log_fields)


async def set_channel_isolation(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    isolated: bool,
    default: DeploymentDefault,
    actor_account_id: uuid.UUID | None,
    channel_label: str | None = None,
    fork: bool = False,
    fork_from: str | None = None,
    public_url: str | None = None,
    drop_seal_and_pins: bool = False,
    subject: Subject,
) -> IsolationChange:
    """Isolate `channel_id` or end its isolation; raise `ChannelIsolationRefused` if refused.

    A copy is made only when `fork` is set and the channel has no agent that may
    be its own, so repeating a request never copies twice; `subject` is who
    asks for it (`authorize(FORK)`). A copy the locked re-check refuses, or
    one whose write fails, is archived unless the channel names it
    (`_archive_unused_copy`).
    """
    if not isolated:
        async with sessionmaker.begin() as session:
            await lock_access_policy(session, tenant_id=tenant_id)
            policy = await load_access_policy(session, tenant_id=tenant_id)
            updated = end_isolation(
                policy, channel_id=channel_id, drop_seal_and_pins=drop_seal_and_pins
            )
            if updated != policy:
                await set_access_policy(session, tenant_id=tenant_id, policy=updated)
        return IsolationChange(
            channel_id,
            False,
            None,
            None,
            updated != policy,
            lifted_seal_and_pins=drop_seal_and_pins,
        )
    known = await _lookup_channel_agent(
        anthropic, sessionmaker, tenant_id=tenant_id, channel_id=channel_id
    )
    async with sessionmaker.begin() as session:
        found, changed = await _write_isolation(
            anthropic,
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            default=default,
            known=known,
        )
    if found.refusal is None:
        return IsolationChange(
            channel_id,
            True,
            found.name,
            None,
            changed,
            network_warning=await _network_warning(
                anthropic, sessionmaker, tenant_id=tenant_id, channel_id=channel_id, default=default
            ),
        )
    source = fork_from or found.answering
    if not fork or source is None:
        raise ChannelIsolationRefused(found.refusal, agent_name=found.name or source)
    taken = {
        agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name
        for agent in await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    }
    new_name = isolated_agent_name(channel_label, channel_id, taken=taken)
    copy = await fork_agent(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        source_name=source,
        new_name=new_name,
        public_url=public_url,
        subject=subject,
        extra_metadata={MA_METADATA_KEY_ISOLATION_COPY: channel_id},
    )
    try:
        async with sessionmaker.begin() as session:
            await lock_access_policy(session, tenant_id=tenant_id)
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
                tenant_id=tenant_id,
                agent_name=new_name,
                mode="agent",
                actor_account_id=actor_account_id,
            )
            found, _ = await _write_isolation(
                anthropic,
                session,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=channel_id,
                default=default,
                known={new_name: copy.agent},
            )
            if found.refusal is not None:
                raise ChannelIsolationRefused(found.refusal, agent_name=new_name)
    except Exception:
        # Re-raised: a copy no channel got, refused or not, must not linger.
        await _archive_unused_copy(
            anthropic,
            sessionmaker,
            tenant_id=tenant_id,
            channel_id=channel_id,
            name=new_name,
            agent_id=copy.agent.id,
        )
        raise
    return IsolationChange(
        channel_id,
        True,
        new_name,
        source,
        True,
        copy.dropped_skills,
        network_warning=await _network_warning(
            anthropic, sessionmaker, tenant_id=tenant_id, channel_id=channel_id, default=default
        ),
    )


__all__ = [
    "END_ISOLATION_WARNING",
    "LIFT_ISOLATION_WARNING",
    "ChannelIsolationRefused",
    "IsolationChange",
    "IsolationRefusal",
    "end_isolation",
    "isolate",
    "isolated_agent_name",
    "isolation_refusal",
    "render_isolation_refusal",
    "set_channel_isolation",
]
