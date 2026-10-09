"""Setting a channel's rule or an agent's, shared by the tool, the panels and the CLI.

A channel's rule says whose turns read it and who posts there; an agent's says
where it runs (`daimon.core.access_policy`). Readers ``own`` keeps a channel
to its own agents: agents whose rule names it alone. Setting it makes the
channel's default agent its own, when that agent is not built in, has no rule
naming another channel and answers nowhere else (`daimon.core.agent_reach`,
every name it carries). Otherwise the request is refused with a reason, unless
a copy is asked for: then `copy_from` (default: whoever answers there now) is
copied (`daimon.core.agent_fork`) under a name taken from the channel and set
as its default. Moving readers off ``own`` keeps its agents' rules unless
`release_agents` drops them. Server admins and operators only
(`authorize(SET_CHANNEL_RULE)`, `SET_AGENT_RULE`), each in one write under
the policy lock.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

import structlog
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import ChannelReaders, ChannelWriters, TenantAccessPolicy
from daimon.core.agent_fork import fork_agent
from daimon.core.agent_pins import agent_pin_names
from daimon.core.agent_reach import load_agent_reach
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.channel_environments import limited_readers_network_warning
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_CHANNEL_COPY,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
)
from daimon.core.errors import DaimonError
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_agent
from daimon.core.permissions import (
    AgentRule,
    ChannelRule,
    RuleRefused,
    agent_permissions,
    channel_rule,
    check_channel_rule,
    runs_only_in,
    with_agent_rule,
    with_category_rule,
    with_channel_rule,
)
from daimon.core.scope import ChannelConfigRow, ChannelScopeRef, DeploymentDefault, pick_agent
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    lock_policy_writes_exclusive,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.scoped_config_read import get_scope, list_propagations_for_tenant
from daimon.core.stores.scoped_config_write import set_fields
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

RuleRefusal = Literal[
    "admin_required",
    "invalid",
    "own_on_both",
    "keeps_own_agents",
    "no_channel_agent",
    "managed_channel_agent",
    "shared_channel_agent",
    "shared_ruled_agent",
    "agent_runs_elsewhere",
    "agent_not_found",
    "agent_has_home",
    "own_channel_alone",
]

READERS_LABELS: Mapping[ChannelReaders, str] = {
    "any": "Anyone",
    "inside": "Only this channel",
    "own": "Only its own agents",
}
"""How the panels name each readers value."""
WRITERS_LABELS: Mapping[ChannelWriters, str] = {
    "any": "Anyone",
    "own": "Only its own agents",
    "none": "Nobody",
}
"""How the panels name each writers value."""


def as_readers(value: object) -> ChannelReaders | None:
    """`value` as a readers choice; None when it isn't one. Pure."""
    for readers in READERS_LABELS:
        if value == readers:
            return readers
    return None


def as_writers(value: object) -> ChannelWriters | None:
    """`value` as a writers choice; None when it isn't one. Pure."""
    for writers in WRITERS_LABELS:
        if value == writers:
            return writers
    return None


_READERS_NOTES: Mapping[ChannelReaders, str] = {
    "any": "any turn may read it",
    "inside": "only turns inside it read it",
    "own": "only its own agents run there, and only turns inside it read it",
}
_WRITERS_NOTES: Mapping[ChannelWriters, str] = {
    "any": "anyone may post there",
    "own": "only its own agents post there",
    "none": "nothing posts there, daimon included",
}


def describe_rule(rule: ChannelRule) -> str:
    """One sentence saying what `rule` does. Pure."""
    return f"{_READERS_NOTES[rule.readers].capitalize()}; {_WRITERS_NOTES[rule.writers]}."


def render_rule_refusal(
    reason: RuleRefusal, *, agent_name: str | None, channel_id: str | None = None
) -> str:
    name = agent_name or "the agent answering here"
    match reason:
        case "admin_required":
            return "Only a server or workspace admin can change channel and agent rules."
        case "invalid":
            return "That rule can't be set there."
        case "own_on_both":
            return (
                "Only a channel read by its own agents alone takes writers own, and such a "
                "channel is posted in by its own agents or by nobody."
            )
        case "keeps_own_agents":
            return (
                "Only its own agents run in this channel, so it keeps them. Change who can "
                "read it first."
            )
        case "no_channel_agent":
            return (
                "This channel has no default agent that could be its own. Ask for a copy of "
                "the agent answering here instead."
            )
        case "managed_channel_agent":
            return (
                f"{name} is built in, so it can't be one channel's own agent. Ask for a copy "
                f"of {name} instead."
            )
        case "shared_channel_agent":
            return (
                f"{name} also answers outside this channel, so it can't be one of its own "
                f"agents. Ask for a copy of {name} instead."
            )
        case "shared_ruled_agent":
            # An agent with a rule can't be copied (`authorize(FORK)`), so no copy is offered.
            return (
                f"{name} also answers outside this channel, so it can't be one of its own "
                "agents, and an agent with a rule can't be copied. Stop it answering "
                "elsewhere first."
            )
        case "agent_runs_elsewhere":
            return (
                f"{name}'s agent rule names other channels, so it can't be this channel's own "
                "agent, and an agent with a rule can't be copied. Change its rule first."
            )
        case "agent_not_found":
            return f"There is no agent named {name}."
        case "agent_has_home":
            return (
                f"{name} is the own agent of {channel_id}, which only its own agents read. "
                "Change that channel's readers first, releasing its agents."
            )
        case "own_channel_alone":
            return (
                f"Only its own agents read {channel_id}, so an agent rule naming it names it alone."
            )


class ChannelRuleRefused(DaimonError):
    """A rule change was refused; the message is person-facing."""

    def __init__(
        self,
        reason: RuleRefusal,
        *,
        agent_name: str | None = None,
        channel_id: str | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(
            message or render_rule_refusal(reason, agent_name=agent_name, channel_id=channel_id)
        )
        self.reason: RuleRefusal = reason


@dataclass(frozen=True)
class RuleChange:
    """What `set_channel_rule` did."""

    channel_id: str
    before: ChannelRule
    rule: ChannelRule
    changed: bool
    agent_name: str | None = None
    """The agent made the channel's own by this change."""
    copied_from: str | None = None
    released: tuple[str, ...] = ()
    """Agents whose rule named this channel alone, dropped by this change."""
    kept: tuple[str, ...] = ()
    """Agents whose rule still names this channel alone, now it isn't kept to them."""
    dropped_skills: tuple[str, ...] = ()
    """Skills left off the copy: another agent's, or the source's own that failed to copy."""
    network_warning: str | None = None
    """Readers were limited while the channel's own environment has an unconfirmed open network."""

    @property
    def notes(self) -> tuple[str, ...]:
        """What the change means, sentence by sentence, for the person who made it."""
        notes = [describe_rule(self.rule)]
        if self.copied_from is not None:
            notes.append(f"{self.agent_name}, a copy of {self.copied_from}, is its own agent.")
        elif self.agent_name is not None:
            notes.append(f"{self.agent_name} is its own agent and runs only there.")
        if self.dropped_skills:
            notes.append(f"Left off the copy: {', '.join(self.dropped_skills)}.")
        if self.kept:
            notes.append(
                f"{', '.join(self.kept)} still run only there, but show elsewhere again; "
                "releasing them lets them run elsewhere, bringing what they remembered there."
            )
        if self.released:
            notes.append(
                f"{', '.join(self.released)} may now run elsewhere, bringing what they "
                "remembered there."
            )
        if self.network_warning is not None:
            notes.append(self.network_warning)
        return tuple(notes)


@dataclass(frozen=True)
class ChannelRuleStatus:
    """A channel's rule and the agents kept to it, as the panels show them."""

    rule: ChannelRule
    agents: tuple[str, ...]
    """Agents whose rule names this channel alone."""

    @property
    def line(self) -> str:
        agents = ", ".join(self.agents) or "none"
        return (
            f"Who can read it: {READERS_LABELS[self.rule.readers]} · Who can post: "
            f"{WRITERS_LABELS[self.rule.writers]} · Agents kept here: {agents}"
        )


def channel_rule_status(policy: TenantAccessPolicy, channel_id: str) -> ChannelRuleStatus:
    """`channel_id`'s rule now. Pure."""
    return ChannelRuleStatus(channel_rule(policy, channel_id), runs_only_in(policy, channel_id))


def resolve_rule(
    current: ChannelRule, *, readers: ChannelReaders | None, writers: ChannelWriters | None
) -> ChannelRule:
    """The rule asked for; a None keeps that side, or follows the other to and from own.

    Raise `ChannelRuleRefused("own_on_both")` for a pair no rule takes. Pure.
    """
    if readers is None:
        readers = "own" if writers == "own" else current.readers
    if writers is None:
        writers = "none" if current.writers == "none" else "own" if readers == "own" else "any"
    try:
        return ChannelRule(readers=readers, writers=writers)
    except ValidationError:
        raise ChannelRuleRefused("own_on_both") from None


def copy_name(channel_label: str | None, channel_id: str, *, taken: Collection[str]) -> str:
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


def keep_to_own(
    policy: TenantAccessPolicy, *, channel_id: str, rule: ChannelRule, agent_name: str
) -> TenantAccessPolicy:
    """`policy` with `rule` (readers own) on the channel and `agent_name` its own agent. Pure."""
    policy = with_channel_rule(policy, channel_id, rule)
    return with_agent_rule(policy, agent_name, AgentRule(runs_in=(channel_id,)))


def release_agents_of(
    policy: TenantAccessPolicy, channel_id: str
) -> tuple[TenantAccessPolicy, tuple[str, ...]]:
    """`policy` without the rule of every agent it runs in `channel_id` alone, and their names."""
    names = runs_only_in(policy, channel_id)
    for name in names:
        policy = with_agent_rule(policy, name, AgentRule())
    return policy, names


async def _own_agent_refusal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    policy: TenantAccessPolicy,
    default: DeploymentDefault,
    name: str,
    agent: BetaManagedAgentsAgent,
    replacing_rule: bool = False,
) -> RuleRefusal | None:
    """Why `agent` can't be `channel_id`'s own agent; None when it can.

    `replacing_rule`: its own rule is being replaced, so only where it answers counts.
    """
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        return "managed_channel_agent"
    names = tuple(n for n in (name, *agent_pin_names(agent.name, agent.metadata)) if n)
    rules = () if replacing_rule else agent_permissions(policy, names).runs_in
    if any(rule - {channel_id} for rule in rules):
        return "agent_runs_elsewhere"
    reach = await load_agent_reach(
        session,
        tenant_id=tenant_id,
        platform=platform,
        agent_names=names,
        ma_agent_id=agent.id,
        default=default,
    )
    if not reach.stays_inside({channel_id}):
        return "shared_ruled_agent" if rules else "shared_channel_agent"
    return None


@dataclass(frozen=True)
class _ChannelAgent:
    name: str | None
    """The agent set as the channel's default, if any."""
    answering: str | None
    """The agent answering in the channel now, by the cascade."""
    refusal: RuleRefusal | None


async def _channel_agent(
    anthropic: AsyncAnthropic,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    policy: TenantAccessPolicy,
    default: DeploymentDefault,
    known: Mapping[str, BetaManagedAgentsAgent | None],
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
        return _ChannelAgent(None, answering, "no_channel_agent")
    agent = (
        known[name]
        if name in known
        else await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=name)
    )
    if agent is None:
        return _ChannelAgent(name, answering, "no_channel_agent")
    refusal = await _own_agent_refusal(
        session,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        policy=policy,
        default=default,
        name=name,
        agent=agent,
    )
    return _ChannelAgent(name, answering, refusal)


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


@dataclass(frozen=True)
class _Ask:
    channel_id: str
    readers: ChannelReaders | None
    writers: ChannelWriters | None
    release_agents: bool
    subject: Subject


async def _write_rule(
    anthropic: AsyncAnthropic,
    session: AsyncSession,
    ask: _Ask,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    default: DeploymentDefault,
    known: Mapping[str, BetaManagedAgentsAgent | None],
) -> tuple[RuleChange, _ChannelAgent | None]:
    """Under the policy lock: write the rule, or say why the channel's agent can't be its own."""
    await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
    await lock_access_policy(session, tenant_id=tenant_id)
    policy = await load_access_policy(session, tenant_id=tenant_id)
    if not authorize(
        policy,
        subject=ask.subject,
        action=Action.SET_CHANNEL_RULE,
        place=Place(channel_id=ask.channel_id),
    ):
        raise ChannelRuleRefused("admin_required")
    before = channel_rule(policy, ask.channel_id)
    rule = resolve_rule(before, readers=ask.readers, writers=ask.writers)
    try:
        check_channel_rule(ask.channel_id, rule)
    except RuleRefused as exc:
        raise ChannelRuleRefused("invalid", message=f"{exc}.") from None
    if ask.release_agents and rule.readers == "own":
        raise ChannelRuleRefused("keeps_own_agents")
    found = None
    released: tuple[str, ...] = ()
    if rule.readers == "own" and before.readers != "own":
        found = await _channel_agent(
            anthropic,
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=ask.channel_id,
            policy=policy,
            default=default,
            known=known,
        )
        if found.refusal is not None or found.name is None:
            return RuleChange(ask.channel_id, before, before, False), found
        updated = keep_to_own(policy, channel_id=ask.channel_id, rule=rule, agent_name=found.name)
    else:
        updated = with_channel_rule(policy, ask.channel_id, rule)
        if ask.release_agents:
            updated, released = release_agents_of(updated, ask.channel_id)
    if updated != policy:
        await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    change = RuleChange(
        ask.channel_id,
        before,
        rule,
        updated != policy,
        agent_name=found.name if found is not None else None,
        released=released,
        kept=runs_only_in(updated, ask.channel_id)
        if before.readers == "own" and rule.readers != "own"
        else (),
    )
    return change, found


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
        _log.warning("channel_rules.copy_kept", reason="unreadable", exc_info=True, **log_fields)
        return
    if isinstance(scope, ChannelConfigRow) and scope.agent_name == name:
        _log.warning("channel_rules.copy_kept", reason="channel_points_at_it", **log_fields)
        return
    try:
        await archive_agent(anthropic, agent_id, scope=resource_scope(tenant_id=str(tenant_id)))
    except APIError:
        _log.warning("channel_rules.copy_archive_failed", exc_info=True, **log_fields)


async def _with_network_warning(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    change: RuleChange,
    *,
    tenant_id: uuid.UUID,
    default: DeploymentDefault,
) -> RuleChange:
    if change.before.readers != "any" or change.rule.readers == "any":
        return change
    async with sessionmaker() as session:
        warning = await limited_readers_network_warning(
            session, anthropic, tenant_id=tenant_id, channel_id=change.channel_id, default=default
        )
    return replace(change, network_warning=warning)


async def set_channel_rule(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    readers: ChannelReaders | None = None,
    writers: ChannelWriters | None = None,
    subject: Subject,
    default: DeploymentDefault,
    actor_account_id: uuid.UUID | None = None,
    copy: bool = False,
    copy_from: str | None = None,
    channel_label: str | None = None,
    public_url: str | None = None,
    release_agents: bool = False,
) -> RuleChange:
    """Set `channel_id`'s rule; raise `ChannelRuleRefused` if refused. None keeps a side.

    A copy is made only when `copy` is set, readers become own and the
    channel has no agent that may be its own, so repeating a request never
    copies twice; `subject` is who asks for it (`authorize(FORK)`). A copy
    the locked re-check refuses, or one whose write fails, is archived unless
    the channel names it (`_archive_unused_copy`).
    """
    ask = _Ask(channel_id, readers, writers, release_agents, subject)
    wants_own = readers == "own" or (readers is None and writers == "own")
    known = (
        await _lookup_channel_agent(
            anthropic, sessionmaker, tenant_id=tenant_id, channel_id=channel_id
        )
        if wants_own
        else {}
    )
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        change, found = await _write_rule(
            anthropic,
            session,
            ask,
            tenant_id=tenant_id,
            platform=platform,
            default=default,
            known=known,
        )
    if found is None or found.refusal is None:
        return await _with_network_warning(
            anthropic, sessionmaker, change, tenant_id=tenant_id, default=default
        )
    source = copy_from or found.answering
    if not copy or source is None:
        raise ChannelRuleRefused(found.refusal, agent_name=found.name or source)
    taken = {
        agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name
        for agent in await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    }
    new_name = copy_name(channel_label, channel_id, taken=taken)
    made = await fork_agent(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        source_name=source,
        new_name=new_name,
        public_url=public_url,
        subject=subject,
        extra_metadata={MA_METADATA_KEY_CHANNEL_COPY: channel_id},
    )
    try:
        async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
            await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
            await lock_access_policy(session, tenant_id=tenant_id)
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
                tenant_id=tenant_id,
                agent_name=new_name,
                mode="agent",
                actor_account_id=actor_account_id,
                set_by_admin=subject.is_admin and not subject.via_agent_key,
            )
            change, found = await _write_rule(
                anthropic,
                session,
                ask,
                tenant_id=tenant_id,
                platform=platform,
                default=default,
                known={new_name: made.agent},
            )
            if found is not None and found.refusal is not None:
                raise ChannelRuleRefused(found.refusal, agent_name=new_name)
    except Exception:
        # Re-raised: a copy no channel got, refused or not, must not linger.
        await _archive_unused_copy(
            anthropic,
            sessionmaker,
            tenant_id=tenant_id,
            channel_id=channel_id,
            name=new_name,
            agent_id=made.agent.id,
        )
        raise
    change = replace(change, copied_from=source, dropped_skills=made.dropped_skills)
    return await _with_network_warning(
        anthropic, sessionmaker, change, tenant_id=tenant_id, default=default
    )


async def set_category_rule(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    category_id: str,
    writers: ChannelWriters,
    subject: Subject,
) -> bool:
    """Set a Discord category's writers (``none`` or ``any``); True when it changed."""
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        await lock_access_policy(session, tenant_id=tenant_id)
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if not authorize(policy, subject=subject, action=Action.SET_CHANNEL_RULE):
            raise ChannelRuleRefused("admin_required")
        try:
            if writers == "own":  # readers own only: a category takes writers none alone
                raise RuleRefused("a category only takes writers none")
            updated = with_category_rule(policy, category_id, ChannelRule(writers=writers))
        except RuleRefused as exc:
            raise ChannelRuleRefused("invalid", message=f"{exc}.") from None
        if updated != policy:
            await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    return updated != policy


@dataclass(frozen=True)
class AgentRuleChange:
    """What `set_agent_rule` did."""

    agent_name: str
    runs_in: tuple[str, ...] | None
    changed: bool
    answers_outside: bool = False
    """It is still set to answer outside its new rule, where every turn is refused."""

    @property
    def notes(self) -> tuple[str, ...]:
        if self.runs_in is None:
            notes = [f"{self.agent_name} runs wherever it is set to answer."]
        elif not self.runs_in:
            notes = [f"{self.agent_name} runs nowhere."]
        else:
            notes = [f"{self.agent_name} runs only in {', '.join(self.runs_in)}."]
        if self.answers_outside:
            notes.append(
                "It is still set to answer outside them, where its turns are now refused; "
                "change those defaults too."
            )
        return tuple(notes)


async def set_agent_rule(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_name: str,
    runs_in: Sequence[str] | None,
    subject: Subject,
    default: DeploymentDefault,
) -> AgentRuleChange:
    """Set where `agent_name` runs (None: wherever it is set to answer).

    The rule replaces any on the agent's other names. A channel's own agent
    keeps its rule until that channel's readers change; a rule naming a
    channel only its own agents read names it alone, and makes the agent one
    of its own only if it could be (`_own_agent_refusal`). Raise
    `ChannelRuleRefused` if refused.
    """
    # Checked first too, so whether an agent exists is no one else's to learn.
    async with sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    if not authorize(policy, subject=subject, action=Action.SET_AGENT_RULE):
        raise ChannelRuleRefused("admin_required")
    agent = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=agent_name)
    if agent is None:
        raise ChannelRuleRefused("agent_not_found", agent_name=agent_name)
    names = tuple(n for n in (agent_name, *agent_pin_names(agent.name, agent.metadata)) if n)
    target = tuple(dict.fromkeys(runs_in)) if runs_in is not None else None
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
        await lock_access_policy(session, tenant_id=tenant_id)
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if not authorize(policy, subject=subject, action=Action.SET_AGENT_RULE):
            raise ChannelRuleRefused("admin_required")
        home = agent_permissions(policy, names).home
        if home is not None and target != (home,):
            raise ChannelRuleRefused("agent_has_home", agent_name=agent_name, channel_id=home)
        own = [c for c in target or () if channel_rule(policy, c).readers == "own"]
        if own and len(target or ()) > 1:
            raise ChannelRuleRefused("own_channel_alone", channel_id=own[0])
        if own and home is None:
            refusal = await _own_agent_refusal(
                session,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=own[0],
                policy=policy,
                default=default,
                name=agent_name,
                agent=agent,
                replacing_rule=True,
            )
            if refusal is not None:
                raise ChannelRuleRefused(refusal, agent_name=agent_name)
        updated = policy
        for name in names:
            updated = with_agent_rule(updated, name, AgentRule())
        updated = with_agent_rule(updated, agent_name, AgentRule(runs_in=target))
        if updated != policy:
            await set_access_policy(session, tenant_id=tenant_id, policy=updated)
        answers_outside = False
        if target is not None:
            reach = await load_agent_reach(
                session,
                tenant_id=tenant_id,
                platform=platform,
                agent_names=names,
                ma_agent_id=agent.id,
                default=default,
            )
            answers_outside = not reach.stays_inside(target)
    return AgentRuleChange(agent_name, target, updated != policy, answers_outside)


__all__ = [
    "as_readers",
    "as_writers",
    "READERS_LABELS",
    "WRITERS_LABELS",
    "AgentRuleChange",
    "ChannelRuleRefused",
    "ChannelRuleStatus",
    "RuleChange",
    "RuleRefusal",
    "channel_rule_status",
    "copy_name",
    "describe_rule",
    "keep_to_own",
    "release_agents_of",
    "render_rule_refusal",
    "resolve_rule",
    "set_agent_rule",
    "set_category_rule",
    "set_channel_rule",
]
