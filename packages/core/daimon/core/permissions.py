"""Channel and agent permissions: one vocabulary over the tenant access policy.

Protected, sealed, confidential (isolated) and pinned are not separate
mechanisms but presets of two rules. A channel rule says who may read a
channel and who may write in it; an agent rule says where an agent may run.
What else an agent may do (where it posts, whom it messages, where it is
listed, whether it may be copied) follows from those two rules and is derived
here, never stored.

Pure. Rules are read from and written back to `TenantAccessPolicy`, whose
stored shape is unchanged, and `daimon.core.authz.authorize` still decides
every action. The views below describe its decisions for a member's turn
(pinned against `authorize` in ``tests/test_permissions.py``), so a panel, a
card or the CLI can show what holds without restating the rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from daimon.core.access_policy import (
    TenantAccessPolicy,
    is_write_protected,
    isolated_channel_of,
    isolation_owner,
)
from pydantic import BaseModel, ConfigDict, model_validator

ChannelReaders = Literal["anyone", "inside", "own_agents"]
"""Who may read a channel: any turn, only a turn inside it (sealed), or only
its own agents, inside it (confidential)."""

ChannelWriters = Literal["any_agent", "own_agents", "nobody"]
"""Who may start turns and post in a channel: any agent, only its own agents
(confidential), or nothing at all, admins and the bot's notices included
(protected)."""

ChannelPreset = Literal["open", "protected", "sealed", "confidential"]

AgentPosts = Literal["anywhere", "runs_in_and_requester_dm", "confidential_channel"]
AgentDirectMessages = Literal["anyone", "requester", "nobody"]
AgentListing = Literal["everywhere", "confidential_channel"]


class ChannelRule(BaseModel):
    """Who may read and write one channel, thread, Slack ``channel:ts`` or category."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    readers: ChannelReaders = "anyone"
    writers: ChannelWriters = "any_agent"

    @model_validator(mode="after")
    def _own_agents_read_and_write(self) -> ChannelRule:
        # A channel's own agents are its only writers only while they are its
        # only readers too, and the reverse unless nothing writes there.
        own_writers_only = self.writers == "own_agents" and self.readers != "own_agents"
        if own_writers_only or (self.readers == "own_agents" and self.writers == "any_agent"):
            raise ValueError("a channel kept to its own agents keeps both reading and writing")
        return self


class AgentRule(BaseModel):
    """Where one agent may run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_in: tuple[str, ...] | None = None
    """The only channels (and threads under them) it runs in. None runs it
    wherever the cascade sends it; an empty tuple runs it nowhere."""


CHANNEL_PRESETS: Mapping[ChannelPreset, ChannelRule] = {
    "open": ChannelRule(),
    "protected": ChannelRule(writers="nobody"),
    "sealed": ChannelRule(readers="inside"),
    "confidential": ChannelRule(readers="own_agents", writers="own_agents"),
}


def preset_of(rule: ChannelRule) -> ChannelPreset | None:
    """The preset a rule is, or None for a mix (a sealed channel also protected)."""
    return next((name for name, preset in CHANNEL_PRESETS.items() if preset == rule), None)


def channel_rule(policy: TenantAccessPolicy, channel_id: str) -> ChannelRule:
    """The rule stored on exactly this id; a thread's channel is not consulted."""
    protected = channel_id in policy.protected_channel_ids
    if channel_id in policy.isolated_channel_ids:
        return ChannelRule(readers="own_agents", writers="nobody" if protected else "own_agents")
    return ChannelRule(
        readers="inside" if channel_id in policy.sealed_channel_ids else "anyone",
        writers="nobody" if protected else "any_agent",
    )


def channel_rules(policy: TenantAccessPolicy) -> dict[str, ChannelRule]:
    """Every id with a rule other than open, in the order the policy names them."""
    ids = dict.fromkeys(
        (*policy.protected_channel_ids, *policy.sealed_channel_ids, *policy.isolated_channel_ids)
    )
    return {channel_id: channel_rule(policy, channel_id) for channel_id in ids}


def category_rules(policy: TenantAccessPolicy) -> dict[str, ChannelRule]:
    """Discord categories with a rule; a category can only be protected."""
    return dict.fromkeys(policy.protected_category_ids, CHANNEL_PRESETS["protected"])


def agent_rules(policy: TenantAccessPolicy) -> dict[str, AgentRule]:
    """Every agent name with a rule (a pin)."""
    return {name: AgentRule(runs_in=pin) for name, pin in policy.agent_channel_pins.items()}


def _toggled(ids: tuple[str, ...], channel_id: str, *, on: bool) -> tuple[str, ...]:
    if on:
        return ids if channel_id in ids else (*ids, channel_id)
    return tuple(i for i in ids if i != channel_id)


def _rebuilt(policy: TenantAccessPolicy, **changes: object) -> TenantAccessPolicy:
    # Validated, never model_copy: the policy's own invariants must hold.
    return TenantAccessPolicy.model_validate({**policy.model_dump(), **changes})


def with_channel_rule(
    policy: TenantAccessPolicy, channel_id: str, rule: ChannelRule
) -> TenantAccessPolicy:
    """The policy with `rule` on `channel_id`; every other id keeps its rule.

    This sets the channel's rule only: making it confidential does not pin an
    agent to it (`with_agent_rule`, or `daimon.core.channel_isolation_setup`).
    """
    return _rebuilt(
        policy,
        protected_channel_ids=_toggled(
            policy.protected_channel_ids, channel_id, on=rule.writers == "nobody"
        ),
        sealed_channel_ids=_toggled(
            policy.sealed_channel_ids, channel_id, on=rule.readers != "anyone"
        ),
        isolated_channel_ids=_toggled(
            policy.isolated_channel_ids, channel_id, on=rule.readers == "own_agents"
        ),
    )


def with_category_rule(
    policy: TenantAccessPolicy, category_id: str, rule: ChannelRule
) -> TenantAccessPolicy:
    """The policy with `rule` on a Discord category. Only open and protected apply."""
    if rule not in (CHANNEL_PRESETS["open"], CHANNEL_PRESETS["protected"]):
        raise ValueError("a category can only be open or protected")
    return _rebuilt(
        policy,
        protected_category_ids=_toggled(
            policy.protected_category_ids, category_id, on=rule.writers == "nobody"
        ),
    )


def with_agent_rule(
    policy: TenantAccessPolicy, agent_name: str, rule: AgentRule
) -> TenantAccessPolicy:
    """The policy with `rule` on one agent name; other names keep theirs."""
    pins = {name: pin for name, pin in policy.agent_channel_pins.items() if name != agent_name}
    if rule.runs_in is not None:
        pins[agent_name] = rule.runs_in
    return _rebuilt(policy, agent_channel_pins=pins)


@dataclass(frozen=True)
class ChannelPermissions:
    """What holds at one place: a channel, or a thread under its channel.

    The strictest of the place's own rule, its channel's, a Slack thread's
    ``channel:ts`` rule and its Discord category's.
    """

    readers: ChannelReaders
    writers: ChannelWriters
    confidential_channel: str | None
    """The confidential channel the place lies in (a thread counts as its channel)."""
    own_agents: tuple[str, ...]
    """Agent names pinned to `confidential_channel` alone, the only ones that
    answer and are listed there. Empty outside a confidential channel."""


def channel_permissions(
    policy: TenantAccessPolicy,
    *,
    channel_id: str,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
) -> ChannelPermissions:
    """The permissions at a place, as `authorize` applies them to a member's turn."""
    confidential = isolated_channel_of(policy, channel_id, parent_channel_id)
    seal_ids = {channel_id, parent_channel_id}
    if parent_channel_id is not None:
        seal_ids.add(f"{parent_channel_id}:{channel_id}")
    sealed = any(i is not None and i in policy.sealed_channel_ids for i in seal_ids)
    protected = is_write_protected(
        policy,
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )
    readers: ChannelReaders = (
        "own_agents" if confidential is not None else "inside" if sealed else "anyone"
    )
    writers: ChannelWriters = (
        "nobody" if protected else "own_agents" if confidential is not None else "any_agent"
    )
    return ChannelPermissions(
        readers=readers,
        writers=writers,
        confidential_channel=confidential,
        own_agents=_pinned_alone(policy, confidential) if confidential is not None else (),
    )


def _pinned_alone(policy: TenantAccessPolicy, channel_id: str) -> tuple[str, ...]:
    pins = policy.agent_channel_pins.items()
    return tuple(sorted(name for name, pin in pins if set(pin) == {channel_id}))


@dataclass(frozen=True)
class AgentPermissions:
    """What one agent may do, from its rule and the rule of the channel it belongs to.

    For a member's turn. A server admin may also run a pinned agent in their
    own DM and hub, where only they see it, and so may a confidential
    channel's admin with that channel's own agents. A turn inside a
    confidential channel holds whatever agent runs it to that channel.
    """

    runs_in: frozenset[str] | None
    """Channels it runs in (and threads under them): inside every pin on any
    of its names. None for an unpinned agent."""
    confidential_channel: str | None
    """The confidential channel it is an own agent of."""
    posts_to: AgentPosts
    direct_messages: AgentDirectMessages
    listed_in: AgentListing
    may_be_copied: bool


def agent_permissions(
    policy: TenantAccessPolicy, agent_names: tuple[str | None, ...]
) -> AgentPermissions:
    """The permissions of the agent carrying `agent_names` (`daimon.core.authz.agent_names`)."""
    pins = [
        frozenset(policy.agent_channel_pins[name])
        for name in agent_names
        if name is not None and name in policy.agent_channel_pins
    ]
    runs_in: frozenset[str] | None = None
    for pin in pins:
        runs_in = pin if runs_in is None else runs_in & pin
    confidential = isolation_owner(policy, agent_names)
    if confidential is not None:
        posts_to: AgentPosts = "confidential_channel"
        direct_messages: AgentDirectMessages = "nobody"
    elif pins:
        posts_to, direct_messages = "runs_in_and_requester_dm", "requester"
    else:
        posts_to, direct_messages = "anywhere", "anyone"
    return AgentPermissions(
        runs_in=runs_in,
        confidential_channel=confidential,
        posts_to=posts_to,
        direct_messages=direct_messages,
        listed_in="confidential_channel" if confidential is not None else "everywhere",
        may_be_copied=not pins,
    )


__all__ = [
    "CHANNEL_PRESETS",
    "AgentDirectMessages",
    "AgentListing",
    "AgentPermissions",
    "AgentPosts",
    "AgentRule",
    "ChannelPermissions",
    "ChannelPreset",
    "ChannelReaders",
    "ChannelRule",
    "ChannelWriters",
    "agent_permissions",
    "agent_rules",
    "category_rules",
    "channel_permissions",
    "channel_rule",
    "channel_rules",
    "preset_of",
    "with_agent_rule",
    "with_category_rule",
    "with_channel_rule",
]
