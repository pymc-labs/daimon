"""Channel and agent permissions: what protected, sealed, confidential and pinned mean.

Two rules hold every limit. A channel rule says whose turns may read a channel
(`readers`) and who may write in it (`writers`), on one scale: ``any``,
``inside`` (a turn in the channel), ``own`` (the channel's own agents) and
``none``. An agent rule says where an agent runs (`AgentRule.runs_in`, a pin).
Protected, sealed and confidential (stored as isolated) are presets of the
channel rule. A confidential channel's own agents are those whose every pin
names that channel alone.

Every other limit follows from the two rules and is defined here once: what
holds at a place (`ChannelPermissions`), what an agent may do
(`AgentPermissions`), and the checks between them (`run_refusal`,
`post_refusal`, `memory_writable`, `listed_at`, `readable_from`, ...).
`daimon.core.authz.authorize` decides through these and adds who is asking:
admin exemptions and agents it couldn't resolve. The stored policy keeps its
shape: rules are read from its lists and written back through them
(`with_channel_rule`, `with_agent_rule`). Pure.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from daimon.core.access_policy import (
    TenantAccessPolicy,
    is_write_protected,
    isolated_channel_of,
    isolation_owner,
)
from daimon.core.errors import DaimonError
from pydantic import BaseModel, ConfigDict, model_validator

ChannelReaders = Literal["any", "inside", "own"]
"""Whose turns may read a channel: any turn, a turn inside it (sealed), or its
own agents' turns inside it (confidential)."""

ChannelWriters = Literal["any", "own", "none"]
"""Who may start turns and post in a channel: any agent, its own agents
(confidential), or nothing at all, admins and daimon's notices included
(protected)."""

ChannelPreset = Literal["open", "protected", "sealed", "confidential"]

AgentKind = Literal["free", "pinned", "own"]
"""Unpinned, pinned, or one of a confidential channel's own agents."""

Recipients = Literal["any", "requester", "none"]

Refusal = Literal["protected", "pinned_elsewhere", "confidential"]
"""Why an agent may not run or post at a place: the channel is protected, a
pin names elsewhere, or a confidential channel's line would be crossed."""

_SLACK_THREAD_KEY = re.compile(r"[CGD][A-Z0-9]+:\d+\.\d+")


class RuleRefused(DaimonError, ValueError):
    """A rule no turn could be held to; the message is person-facing."""


class ChannelRule(BaseModel):
    """Who may read and write one channel, thread, Slack ``channel:ts`` or category."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    readers: ChannelReaders = "any"
    writers: ChannelWriters = "any"

    @model_validator(mode="after")
    def _own_on_both(self) -> ChannelRule:
        # Only a channel kept to its own agents has own agents to write, and
        # one kept to them is written by them or by nothing.
        if (self.writers == "own") != (self.readers == "own") and self.writers != "none":
            raise ValueError("`own` goes on both readers and writers, or readers with writers none")
        return self


class AgentRule(BaseModel):
    """Where one agent may run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_in: tuple[str, ...] | None = None
    """The only channels (and threads under them) it runs in. None runs it
    wherever the cascade sends it; an empty tuple runs it nowhere."""


CHANNEL_PRESETS: Mapping[ChannelPreset, ChannelRule] = {
    "open": ChannelRule(),
    "protected": ChannelRule(writers="none"),
    "sealed": ChannelRule(readers="inside"),
    "confidential": ChannelRule(readers="own", writers="own"),
}


def preset_of(rule: ChannelRule) -> ChannelPreset | None:
    """The preset a rule is, or None for a mix (a sealed channel also protected)."""
    return next((name for name, preset in CHANNEL_PRESETS.items() if preset == rule), None)


def channel_rule(policy: TenantAccessPolicy, channel_id: str) -> ChannelRule:
    """The rule stored on exactly this id; a thread's channel is not consulted."""
    protected = channel_id in policy.protected_channel_ids
    if channel_id in policy.isolated_channel_ids:
        return ChannelRule(readers="own", writers="none" if protected else "own")
    return ChannelRule(
        readers="inside" if channel_id in policy.sealed_channel_ids else "any",
        writers="none" if protected else "any",
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

    This sets the channel's rule only: making it confidential pins no agent to
    it (`with_agent_rule`, or `daimon.core.channel_isolation_setup`).
    Confidential is meant for channels; their threads follow them. Raise
    `RuleRefused` for a rule `check_channel_rule` refuses.
    """
    check_channel_rule(channel_id, rule)
    return _rebuilt(
        policy,
        protected_channel_ids=_toggled(
            policy.protected_channel_ids, channel_id, on=rule.writers == "none"
        ),
        sealed_channel_ids=_toggled(
            policy.sealed_channel_ids, channel_id, on=rule.readers != "any"
        ),
        isolated_channel_ids=_toggled(
            policy.isolated_channel_ids, channel_id, on=rule.readers == "own"
        ),
    )


def check_channel_rule(channel_id: str, rule: ChannelRule) -> None:
    """Raise `RuleRefused` for a rule nothing would enforce at `channel_id`.

    A Slack thread (``channel:ts``) can only be sealed: a turn there is
    protected or kept inside by its channel.
    """
    if _SLACK_THREAD_KEY.fullmatch(channel_id) and rule not in (
        CHANNEL_PRESETS["open"],
        CHANNEL_PRESETS["sealed"],
    ):
        raise RuleRefused(
            f"{channel_id} is a Slack thread, which can only be sealed; "
            "protect or isolate its channel instead"
        )


def with_category_rule(
    policy: TenantAccessPolicy, category_id: str, rule: ChannelRule
) -> TenantAccessPolicy:
    """The policy with `rule` on a Discord category. Only open and protected apply."""
    if rule not in (CHANNEL_PRESETS["open"], CHANNEL_PRESETS["protected"]):
        raise RuleRefused("a category can only be open or protected")
    return _rebuilt(
        policy,
        protected_category_ids=_toggled(
            policy.protected_category_ids, category_id, on=rule.writers == "none"
        ),
    )


def with_agent_rule(
    policy: TenantAccessPolicy, agent_name: str, rule: AgentRule
) -> TenantAccessPolicy:
    """The policy with `rule` on one agent name; other names keep theirs, in order."""
    pins = dict(policy.agent_channel_pins)
    if rule.runs_in is None:
        pins.pop(agent_name, None)
    else:
        pins[agent_name] = rule.runs_in
    return _rebuilt(policy, agent_channel_pins=pins)


def pinned_alone(policy: TenantAccessPolicy, channel_id: str) -> tuple[str, ...]:
    """Names whose pin names `channel_id` alone, sorted: its own agents once it is confidential."""
    pins = policy.agent_channel_pins.items()
    return tuple(sorted(name for name, pin in pins if set(pin) == {channel_id}))


@dataclass(frozen=True)
class ChannelPermissions:
    """What the rules say at one place: a channel, or a thread under its channel.

    The strictest of the place's own rule, its channel's, a Slack thread's
    ``channel:ts`` rule and its Discord category's. No channel at all (a DM, a
    headless call) is open.
    """

    channel_id: str | None = None
    parent_channel_id: str | None = None
    readers: ChannelReaders = "any"
    writers: ChannelWriters = "any"
    confidential_channel: str | None = None
    """The confidential channel the place lies in (a thread counts as its channel)."""
    confidential_unknown: bool = False
    """A thread whose channel isn't known while a channel is confidential: it
    may lie in one, so it is treated as one with no own agents."""

    @property
    def keeps_content(self) -> bool:
        """Whether whatever a turn here writes stays here: posts, direct messages,
        publishing, new agents and routine results (`held_to`)."""
        return self.readers == "own" or self.confidential_unknown


def channel_permissions(
    policy: TenantAccessPolicy,
    *,
    channel_id: str | None,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
    parent_unresolved: bool = False,
) -> ChannelPermissions:
    """The permissions at a place. Pass a thread's channel as `parent_channel_id`.

    `parent_unresolved` marks a thread whose channel isn't known: while any
    channel is confidential it fails closed (`confidential_unknown`).
    """
    if channel_id is None and parent_channel_id is None and not parent_unresolved:
        return ChannelPermissions()
    confidential = isolated_channel_of(policy, channel_id, parent_channel_id)
    ids = {channel_id, parent_channel_id}
    if channel_id is not None and parent_channel_id is not None:
        ids.add(f"{parent_channel_id}:{channel_id}")
    sealed = any(i is not None and i in policy.sealed_channel_ids for i in ids)
    protected = channel_id is not None and is_write_protected(
        policy,
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )
    own = confidential is not None
    readers: ChannelReaders = "own" if own else "inside" if sealed else "any"
    writers: ChannelWriters = "none" if protected else "own" if own else "any"
    return ChannelPermissions(
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        readers=readers,
        writers=writers,
        confidential_channel=confidential,
        confidential_unknown=(
            parent_unresolved and confidential is None and bool(policy.isolated_channel_ids)
        ),
    )


@dataclass(frozen=True)
class AgentPermissions:
    """What one agent may do, from its rule and the rule of the channel it belongs to.

    For a member's turn. A server admin may also run a pinned agent in their own
    DM and hub, where only they see it, and so may a confidential channel's
    admin with that channel's own agents. A turn inside a confidential channel
    keeps whatever agent runs it to that channel (`held_to`).
    """

    pins: tuple[frozenset[str], ...] = ()
    """Every pin on any of its names; it runs only where each one allows. Empty
    for a free agent."""
    own_channel: str | None = None
    """The confidential channel it is one of the own agents of."""

    @property
    def kind(self) -> AgentKind:
        if self.own_channel is not None:
            return "own"
        return "pinned" if self.pins else "free"

    def direct_messages(self, origin: ChannelPermissions | None = None) -> Recipients:
        """Whom a call running as it, from a turn at `origin`, may send a direct message."""
        if held_to(self, origin) is not None:
            return "none"
        return "requester" if self.pins else "any"

    def publishes(self, origin: ChannelPermissions | None = None) -> bool:
        """Whether such a call may publish (a report, an upload URL, daimon's display
        identity), which anyone holding the link reads."""
        return held_to(self, origin) is None and not self.pins

    def creates_agents(self, origin: ChannelPermissions | None = None) -> bool:
        """Whether such a call may create an agent, which would answer elsewhere."""
        return held_to(self, origin) is None

    @property
    def may_be_copied(self) -> bool:
        """Whether a server admin may fork it; a pinned agent is never copied."""
        return not self.pins

    @property
    def budget_channel(self) -> str | None:
        """The channel whose budget every run is charged to; None charges the
        channel the run is in."""
        return self.own_channel

    def pinned_within(self, channel_ids: Collection[str]) -> bool:
        """Whether every pin on it names only `channel_ids`; False when unpinned
        or pinned nowhere."""
        return bool(self.pins) and all(pin and pin <= set(channel_ids) for pin in self.pins)


def agent_permissions(
    policy: TenantAccessPolicy, agent_names: Iterable[str | None]
) -> AgentPermissions:
    """The permissions of the agent carrying `agent_names` (`daimon.core.authz.agent_names`)."""
    names = tuple(agent_names)
    return AgentPermissions(
        pins=tuple(
            frozenset(policy.agent_channel_pins[name])
            for name in names
            if name is not None and name in policy.agent_channel_pins
        ),
        own_channel=isolation_owner(policy, names),
    )


def outside_pins(
    agent: AgentPermissions, channel_id: str | None, parent_channel_id: str | None = None
) -> bool:
    """Whether some pin on the agent names neither the place nor its channel.

    No channel (a DM, a headless call) is outside every pin.
    """
    place = {channel_id, parent_channel_id}
    return any(not (place & pin) for pin in agent.pins)


def run_refusal(
    agent: AgentPermissions, here: ChannelPermissions, *, setup_thread: bool = False
) -> Refusal | None:
    """Why a member's turn may not run the agent here; None when it may.

    Inside every pin, and inside a confidential channel only as one of its own
    agents, or in its setup thread. Protection stops the turn, not the agent.
    """
    if outside_pins(agent, here.channel_id, here.parent_channel_id):
        return "pinned_elsewhere"
    if here.confidential_unknown:
        return "confidential"
    if not setup_thread and agent.own_channel != here.confidential_channel:
        return "confidential"
    return None


def runs_at(
    agent: AgentPermissions, here: ChannelPermissions, *, setup_thread: bool = False
) -> bool:
    return run_refusal(agent, here, setup_thread=setup_thread) is None


def held_to(agent: AgentPermissions, origin: ChannelPermissions | None = None) -> str | None:
    """The confidential channel whose content a call carries: the agent's own,
    else the one its turn runs in."""
    if agent.own_channel is not None:
        return agent.own_channel
    return origin.confidential_channel if origin is not None else None


def crosses_confidential(
    agent: AgentPermissions,
    here: ChannelPermissions,
    origin: ChannelPermissions | None = None,
    *,
    setup_origin: bool = False,
) -> bool:
    """Whether acting here would carry content across a confidential channel's line.

    True for a call held to C (`held_to`) acting outside C, and for any agent
    but C's own acting inside C, unless its turn runs in C's setup thread.
    """
    held = held_to(agent, origin)
    if held != here.confidential_channel:
        return True
    return held is not None and agent.own_channel != held and not setup_origin


def post_refusal(
    agent: AgentPermissions,
    here: ChannelPermissions,
    origin: ChannelPermissions | None = None,
    *,
    setup_origin: bool = False,
) -> Refusal | None:
    """Why the agent may not post here from a turn at `origin` (None: no turn place).

    The requester's own DM is outside every pin, but a pinned agent posts
    there; `daimon.core.authz` lets it.
    """
    if here.writers == "none":
        return "protected"
    if here.confidential_unknown or crosses_confidential(
        agent, here, origin, setup_origin=setup_origin
    ):
        return "confidential"
    if outside_pins(agent, here.channel_id, here.parent_channel_id):
        return "pinned_elsewhere"
    return None


def posts_at(
    agent: AgentPermissions,
    here: ChannelPermissions,
    origin: ChannelPermissions | None = None,
    *,
    setup_origin: bool = False,
) -> bool:
    return post_refusal(agent, here, origin, setup_origin=setup_origin) is None


def at_home(agent: AgentPermissions, here: ChannelPermissions) -> bool:
    """Whether the agent is one of the own agents of the confidential channel `here` lies in."""
    return here.confidential_channel is not None and agent.own_channel == here.confidential_channel


def memory_writable(agent: AgentPermissions, here: ChannelPermissions) -> bool:
    """Whether a turn here may write the agent's memory, which is read wherever it runs.

    Not where readers are kept in, except for a confidential channel's own
    agents in it (`at_home`), whose memory is read nowhere else. A DM's memory
    follows the tenant's `dm_memory_read_only`.
    """
    return here.readers == "any" or at_home(agent, here)


def listed_at(agent: AgentPermissions, inside: str | None) -> bool:
    """Whether a member standing in confidential channel `inside` (None: outside
    all of them) sees the agent listed."""
    return agent.own_channel == inside


def readable_from(
    policy: TenantAccessPolicy,
    origin_ids: Collection[str],
    channel_id: str,
    parent_channel_id: str | None = None,
) -> bool:
    """Whether a channel (or a thread under it) is readable from a turn whose ids
    are `origin_ids`: a sealed one only from inside it."""
    sealed = policy.sealed_channel_ids
    if channel_id in sealed:
        return channel_id in origin_ids
    if parent_channel_id is not None and parent_channel_id in sealed:
        return parent_channel_id in origin_ids
    return True


def session_readable_from(
    policy: TenantAccessPolicy,
    origin_ids: Collection[str],
    *,
    channel: str | None,
    thread: str | None,
    seal_ids: Collection[str],
    legacy_thread_id: str | None,
) -> bool:
    """Whether a recorded session is readable from a turn whose ids are `origin_ids`.

    A stamped session reads like its channel and thread under the current
    policy, and stays inside every id that sealed it after an unseal. An
    unstamped session a thread ran on reads only inside that thread while the
    tenant seals anything; one no thread ran on is headless.
    """
    if channel is not None:
        if not set(seal_ids) <= set(origin_ids):
            return False
        if thread is None:
            return readable_from(policy, origin_ids, channel)
        # A Slack thread is sealed on its own as channel_id:thread_ts.
        return readable_from(policy, origin_ids, thread, channel) and readable_from(
            policy, origin_ids, f"{channel}:{thread}", channel
        )
    if legacy_thread_id is None or not policy.sealed_channel_ids:
        return True
    return legacy_thread_id in origin_ids


def session_confidential_channels(
    policy: TenantAccessPolicy, *, channel: str | None, thread: str | None, seal_ids: Iterable[str]
) -> set[str]:
    """The confidential channels a recorded session lies in: by every id that
    sealed it, and by the channel it ran in under the current policy."""
    found = {
        isolated_channel_of(
            policy, seal_id, channel if seal_id == thread else seal_id.partition(":")[0]
        )
        for seal_id in seal_ids
    }
    found.add(isolated_channel_of(policy, channel))
    return {each for each in found if each is not None}


def sealed_under(policy: TenantAccessPolicy, channel: str, thread_ids: Iterable[str]) -> bool:
    """Whether `channel` or a thread under it is sealed: a Slack ``channel:ts`` by
    its key, a Discord thread when `thread_ids` names it."""
    sealed = policy.sealed_channel_ids
    if channel in sealed or any(key.startswith(f"{channel}:") for key in sealed):
        return True
    return any(thread and thread in sealed for thread in thread_ids)


__all__ = [
    "CHANNEL_PRESETS",
    "AgentKind",
    "AgentPermissions",
    "AgentRule",
    "ChannelPermissions",
    "ChannelPreset",
    "ChannelReaders",
    "ChannelRule",
    "ChannelWriters",
    "Recipients",
    "Refusal",
    "RuleRefused",
    "agent_permissions",
    "agent_rules",
    "at_home",
    "category_rules",
    "channel_permissions",
    "channel_rule",
    "channel_rules",
    "check_channel_rule",
    "crosses_confidential",
    "held_to",
    "listed_at",
    "memory_writable",
    "outside_pins",
    "pinned_alone",
    "post_refusal",
    "posts_at",
    "preset_of",
    "readable_from",
    "run_refusal",
    "runs_at",
    "sealed_under",
    "session_confidential_channels",
    "session_readable_from",
    "with_agent_rule",
    "with_category_rule",
    "with_channel_rule",
]
