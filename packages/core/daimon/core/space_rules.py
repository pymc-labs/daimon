"""Creating, changing and deleting Spaces, under the policy lock.

A Space is channels of one tenant sharing one audience and one rule, readers
``inside`` or ``own`` (`daimon.core.permissions`). Its rule is also written
onto each member channel's own rule, so a process on a build without Spaces
still keeps every member contained: an agent naming two members has no home
there and is refused, never let out. Server admins and operators only
(`authorize(SET_SPACE)`). Whether two channels really share an audience is the
caller's to confirm; daimon cannot see who reads a channel.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass

import structlog
from anthropic import AsyncAnthropic
from daimon.core.access_policy import ChannelRule, SpaceRule, TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.channel_rules import own_agent_refusal, render_rule_refusal
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.permissions import (
    AgentRule,
    RuleRefused,
    channel_rule,
    is_thread_key,
    runs_only_in,
    with_agent_rule,
    with_space,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import spaces as store
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    lock_policy_writes_exclusive,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.domain import SpaceRow
from daimon.core.stores.spaces import SpaceRefused
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SpaceChange:
    """What a Space change did."""

    space: SpaceRow
    channel_ids: tuple[str, ...]
    """Its channels afterwards."""
    agents: tuple[str, ...] = ()
    """Agents whose rule the change extended to, or dropped, the channel."""
    rules_before: tuple[tuple[str, ChannelRule], ...] = ()
    """Each channel that joined, with its rule before it took the Space's."""
    kept: tuple[str, ...] = ()
    """Agents left one channel's own, still remembering what they read across the Space."""


def _space_rule(space: SpaceRow, channel_ids: Sequence[str]) -> SpaceRule:
    rule = ChannelRule(readers=space.readers, writers=space.writers)
    return SpaceRule(
        key=store.space_key(space.id),
        name=space.name,
        rule=rule,
        channel_ids=frozenset(channel_ids),
    )


def _own_agents(policy: TenantAccessPolicy, space: SpaceRule) -> tuple[str, ...]:
    if space.rule.readers != "own" or not space.channel_ids:
        return ()
    return runs_only_in(policy, min(space.channel_ids))


def loosened(before: ChannelRule, after: ChannelRule) -> tuple[str, ...]:
    """What `after` gives up of `before`: readers own, or writers none. Pure."""
    lost: list[str] = []
    if before.readers == "own" and after.readers != "own":
        lost.append(f"readers own to {after.readers}")
    if before.writers == "none" and after.writers != "none":
        lost.append(f"writers none to {after.writers}")
    return tuple(lost)


def join_refusal(
    policy: TenantAccessPolicy, space: SpaceRule, channel_id: str, *, loosen_rules: bool = False
) -> str | None:
    """Why `channel_id` may not join `space`; None when it may. Pure.

    Joining replaces the channel's rule with the Space's, so loosening it must
    be asked for (`loosen_rules`). Two own channels each keeping their own
    agents would merge two agents' memory into one audience, which must be
    asked for, not implied.
    """
    if is_thread_key(channel_id):
        return f"{channel_id} is a thread; a Space takes whole channels"
    before = channel_rule(policy, channel_id)
    if not loosen_rules and (lost := loosened(before, space.rule)):
        return (
            f"joining Space {space.name} would change {channel_id} from {'; '.join(lost)}; "
            "ask to loosen its rule (--loosen-rules)"
        )
    if space.rule.readers != "own" or before.readers != "own":
        return None
    joining, staying = runs_only_in(policy, channel_id), _own_agents(policy, space)
    if joining and staying and set(joining) != set(staying):
        return (
            f"{channel_id} keeps its own agents ({', '.join(joining)}) and Space {space.name} "
            f"keeps {', '.join(staying)}; release one channel's agents first"
        )
    return None


async def _locked(
    session: AsyncSession, *, tenant_id: uuid.UUID, subject: Subject
) -> TenantAccessPolicy:
    await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
    await lock_access_policy(session, tenant_id=tenant_id)
    policy = await load_access_policy(session, tenant_id=tenant_id)
    if not authorize(policy, subject=subject, action=Action.SET_SPACE):
        raise SpaceRefused("only a server or workspace admin can change Spaces")
    return policy


async def _space(session: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> SpaceRow:
    space = await store.get_space(session, tenant_id=tenant_id, name=name)
    if space is None:
        raise SpaceRefused(f"there is no Space named {name}")
    return space


async def _members(session: AsyncSession, space: SpaceRow) -> list[str]:
    return [
        ref.channel_id
        for ref in await store.list_space_channels(
            session, tenant_id=space.tenant_id, space_id=space.id
        )
    ]


@dataclass(frozen=True)
class _Agents:
    """What checking a would-be own agent needs (`own_agent_refusal`)."""

    anthropic: AsyncAnthropic
    platform: str
    default: DeploymentDefault


def _newly_own(
    policy: TenantAccessPolicy, updated: TenantAccessPolicy, space: SpaceRule, channel_id: str
) -> tuple[str, ...]:
    """Agents the join makes the Space's own that were no channel's or Space's own before."""
    if space.rule.readers != "own":
        return ()
    was_own = set(_own_agents(policy, space))
    if channel_rule(policy, channel_id).readers == "own":
        was_own |= set(runs_only_in(policy, channel_id))
    return tuple(n for n in runs_only_in(updated, channel_id) if n not in was_own)


async def _require_own_agents(
    session: AsyncSession,
    agents: _Agents,
    *,
    tenant_id: uuid.UUID,
    policy: TenantAccessPolicy,
    space: SpaceRule,
    channel_id: str,
    names: Sequence[str],
) -> None:
    """Refuse a join that makes an agent the Space's own that could not be a channel's.

    The same checks as setting a channel's readers to own: not built in, and
    answering nowhere outside the Space. An agent left only as a rule is skipped.
    """
    for name in names:
        # An MA lookup under the policy lock, but only for an agent the join
        # makes own, which is rare.
        agent = await find_agent_by_daimon_tag(agents.anthropic, tenant_id=tenant_id, name=name)
        if agent is None:
            continue
        refusal = await own_agent_refusal(
            session,
            tenant_id=tenant_id,
            platform=agents.platform,
            channel_id=channel_id,
            policy=policy,
            default=agents.default,
            name=name,
            agent=agent,
            within=space.channel_ids,
        )
        if refusal is not None:
            raise SpaceRefused(
                f"joining {channel_id} would make {name} an own agent of Space {space.name}: "
                + render_rule_refusal(refusal, agent_name=name, channel_id=channel_id)
            )


async def _join(
    session: AsyncSession,
    policy: TenantAccessPolicy,
    agents: _Agents,
    *,
    space: SpaceRow,
    members: list[str],
    channel_id: str,
    extend_agents: bool,
    loosen_rules: bool,
) -> tuple[TenantAccessPolicy, tuple[str, ...]]:
    before = _space_rule(space, members)
    if refusal := join_refusal(policy, before, channel_id, loosen_rules=loosen_rules):
        raise SpaceRefused(refusal)
    extended = _own_agents(policy, before) if extend_agents else ()
    await store.add_space_channel(
        session, space=space, tenant_id=space.tenant_id, channel_id=channel_id
    )
    members.append(channel_id)
    after = _space_rule(space, members)
    try:
        updated = with_space(policy, before.key, after)
    except RuleRefused as exc:
        raise SpaceRefused(str(exc)) from exc
    await _require_own_agents(
        session,
        agents,
        tenant_id=space.tenant_id,
        policy=updated,
        space=after,
        channel_id=channel_id,
        names=_newly_own(policy, updated, before, channel_id),
    )
    for name in extended:
        runs_in = updated.agent_rules[name].runs_in or ()
        updated = with_agent_rule(updated, name, AgentRule(runs_in=(*runs_in, channel_id)))
    return updated, extended


async def create_space(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    name: str,
    rule: ChannelRule,
    channel_ids: Sequence[str],
    subject: Subject,
    default: DeploymentDefault,
    loosen_rules: bool = False,
) -> SpaceChange:
    """Create Space `name` of `channel_ids`; raise `SpaceRefused` if refused.

    `loosen_rules` lets a channel whose rule is stricter than the Space's join.
    """
    agents = _Agents(anthropic, platform, default)
    if rule.readers not in ("inside", "own"):
        raise SpaceRefused("a Space's readers are inside or own")
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        policy = await _locked(session, tenant_id=tenant_id, subject=subject)
        space = await store.create_space(session, tenant_id=tenant_id, name=name, rule=rule)
        members: list[str] = []
        updated = with_space(policy, store.space_key(space.id), _space_rule(space, members))
        for channel_id in dict.fromkeys(channel_ids):
            updated, _ = await _join(
                session,
                updated,
                agents,
                space=space,
                members=members,
                channel_id=channel_id,
                extend_agents=False,
                loosen_rules=loosen_rules,
            )
        await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    _log.info("space.created", tenant_id=str(tenant_id), space_id=str(space.id))
    before = tuple((c, channel_rule(policy, c)) for c in members)
    return SpaceChange(space, tuple(members), rules_before=before)


async def add_space_channel(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    name: str,
    channel_id: str,
    subject: Subject,
    default: DeploymentDefault,
    extend_agents: bool = False,
    loosen_rules: bool = False,
) -> SpaceChange:
    """Add `channel_id` to Space `name`, its rule becoming the Space's.

    `extend_agents` adds the channel to the rule of each of the Space's own
    agents, so they answer there too; `loosen_rules` lets a channel whose rule
    is stricter than the Space's join.
    """
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        policy = await _locked(session, tenant_id=tenant_id, subject=subject)
        space = await _space(session, tenant_id=tenant_id, name=name)
        members = await _members(session, space)
        updated, extended = await _join(
            session,
            policy,
            _Agents(anthropic, platform, default),
            space=space,
            members=members,
            channel_id=channel_id,
            extend_agents=extend_agents,
            loosen_rules=loosen_rules,
        )
        await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    _log.info("space.channel_added", tenant_id=str(tenant_id), space_id=str(space.id))
    before = ((channel_id, channel_rule(policy, channel_id)),)
    return SpaceChange(space, tuple(members), extended, before)


def _keeping(
    policy: TenantAccessPolicy, own: Sequence[str], channel_ids: Collection[str]
) -> tuple[str, ...]:
    """Of `own`, the agents whose rule names a single channel, one of `channel_ids`."""
    return tuple(
        a
        for a in own
        if len(runs := set(policy.agent_rules[a].runs_in or ())) == 1 and runs <= set(channel_ids)
    )


def _require_keeping(kept: Sequence[str], keep_agents: bool, what: str) -> None:
    if kept and not keep_agents:
        raise SpaceRefused(
            f"{', '.join(kept)} would stay {what}'s own agent, still remembering what it read "
            "across the Space; ask to keep them (--keep-agents) or change their rule first"
        )


async def remove_space_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    channel_id: str,
    subject: Subject,
    keep_agents: bool = False,
) -> SpaceChange:
    """Take `channel_id` out of Space `name`.

    The channel keeps the Space's rule, so it stays contained. The Space's own
    agents naming other members too drop it from their rule, keeping the Space
    as their home. One naming only this channel stays its own agent, with what
    it read across the Space in its memory, so that needs `keep_agents`.
    """
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        policy = await _locked(session, tenant_id=tenant_id, subject=subject)
        space = await _space(session, tenant_id=tenant_id, name=name)
        members = await _members(session, space)
        if channel_id not in members:
            raise SpaceRefused(f"{channel_id} is not in Space {name}")
        before = _space_rule(space, members)
        own = _own_agents(policy, before)
        kept = _keeping(policy, own, {channel_id})
        _require_keeping(kept, keep_agents, channel_id)
        await store.remove_space_channel(
            session, space_id=space.id, tenant_id=tenant_id, channel_id=channel_id
        )
        members.remove(channel_id)
        updated = with_space(policy, before.key, _space_rule(space, members))
        dropped: list[str] = []
        for agent in own:
            runs_in = updated.agent_rules[agent].runs_in or ()
            if channel_id in runs_in and len(runs_in) > 1:
                rest = tuple(c for c in runs_in if c != channel_id)
                updated = with_agent_rule(updated, agent, AgentRule(runs_in=rest))
                dropped.append(agent)
        await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    _log.info("space.channel_removed", tenant_id=str(tenant_id), space_id=str(space.id))
    return SpaceChange(space, tuple(members), tuple(dropped), kept=kept)


async def delete_space(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    subject: Subject,
    keep_agents: bool = False,
) -> SpaceChange:
    """Delete Space `name`. Its channels keep its rule, each on its own; an agent
    whose rule names several of them has no home left and runs nowhere. One
    naming a single member stays its own agent, with what it read across the
    Space in its memory, so that needs `keep_agents`."""
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        policy = await _locked(session, tenant_id=tenant_id, subject=subject)
        space = await _space(session, tenant_id=tenant_id, name=name)
        members = await _members(session, space)
        own = _own_agents(policy, _space_rule(space, members))
        kept = _keeping(policy, own, members)
        _require_keeping(kept, keep_agents, "its channel")
        stranded = tuple(a for a in own if a not in kept)
        await store.delete_space(session, tenant_id=tenant_id, space_id=space.id)
    _log.info("space.deleted", tenant_id=str(tenant_id), space_id=str(space.id))
    return SpaceChange(space, tuple(members), stranded, kept=kept)


__all__ = [
    "SpaceChange",
    "SpaceRefused",
    "add_space_channel",
    "create_space",
    "delete_space",
    "join_refusal",
    "loosened",
    "remove_space_channel",
]
