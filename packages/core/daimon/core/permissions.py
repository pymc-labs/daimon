"""Channel and agent rules, and everything that follows from them.

Two rules hold every limit. A channel rule (`ChannelRule`) says whose turns
may read a channel (`readers`: ``any``, ``inside`` it, or its ``own`` agents)
and who may write in it (`writers`: ``any``, its ``own`` agents, or ``none``).
An agent rule (`AgentRule.runs_in`) says the only channels an agent runs in.
A channel's own agents are those whose rule names that channel alone; they
are its own only while its readers are ``own``, which makes it their home.

Every other limit is defined here once: what holds at a place
(`ChannelPermissions`), what an agent may do (`AgentPermissions`), and the
checks between them (`run_refusal`, `post_refusal`, `memory_writable`,
`listed_at`, `readable_from`, ...). `daimon.core.authz.authorize` decides
through these and adds who is asking: admin exemptions and agents it
couldn't resolve. Writes go through `with_channel_rule`, `with_category_rule`
and `with_agent_rule`. Pure.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from daimon.core.access_policy import (
    OPEN_RULE,
    AgentRule,
    ChannelReaders,
    ChannelRule,
    ChannelWriters,
    TenantAccessPolicy,
)
from daimon.core.errors import DaimonError

Recipients = Literal["any", "requester", "none"]

Refusal = Literal["writers_none", "runs_elsewhere", "own_agents_only"]
"""Why an agent may not run or post at a place: the channel's writers are
none, its agent rule names other channels, or a channel kept to its own agents
would be crossed."""

_SLACK_THREAD_KEY = re.compile(r"[CGD][A-Z0-9]+:\d+\.\d+")


class RuleRefused(DaimonError, ValueError):
    """A rule no turn could be held to; the message is person-facing."""


def channel_rule(policy: TenantAccessPolicy, channel_id: str) -> ChannelRule:
    """The rule stored on exactly this id; a thread's channel is not consulted."""
    return policy.channel_rules.get(channel_id, OPEN_RULE)


def _rebuilt(policy: TenantAccessPolicy, **changes: object) -> TenantAccessPolicy:
    # Validated, never model_copy: the policy's own invariants must hold.
    return TenantAccessPolicy.model_validate({**policy.model_dump(), **changes})


def _with(rules: dict[str, ChannelRule], key: str, rule: ChannelRule) -> dict[str, ChannelRule]:
    updated = dict(rules)
    if rule == OPEN_RULE:
        updated.pop(key, None)
    else:
        updated[key] = rule
    return updated


def with_channel_rule(
    policy: TenantAccessPolicy, channel_id: str, rule: ChannelRule
) -> TenantAccessPolicy:
    """The policy with `rule` on `channel_id`; every other id keeps its rule.

    This sets the channel's rule only: an agent becomes its own through its
    agent rule (`with_agent_rule`, or `daimon.core.channel_rules`). Raise
    `RuleRefused` for a rule `check_channel_rule` refuses.
    """
    check_channel_rule(channel_id, rule)
    return _rebuilt(policy, channel_rules=_with(policy.channel_rules, channel_id, rule))


def check_channel_rule(channel_id: str, rule: ChannelRule) -> None:
    """Raise `RuleRefused` for a rule nothing would enforce at `channel_id`.

    A Slack thread (``channel:ts``) only takes readers ``inside``: who writes
    there, and a channel kept to its own agents, follow its channel.
    """
    if _SLACK_THREAD_KEY.fullmatch(channel_id) and rule not in (
        OPEN_RULE,
        ChannelRule(readers="inside"),
    ):
        raise RuleRefused(
            f"{channel_id} is a Slack thread, which only takes readers inside; "
            "set the rule on its channel instead"
        )


def with_category_rule(
    policy: TenantAccessPolicy, category_id: str, rule: ChannelRule
) -> TenantAccessPolicy:
    """The policy with `rule` on a Discord category. Only writers none applies."""
    if rule not in (OPEN_RULE, ChannelRule(writers="none")):
        raise RuleRefused("a category only takes writers none")
    return _rebuilt(policy, category_rules=_with(policy.category_rules, category_id, rule))


def with_agent_rule(
    policy: TenantAccessPolicy, agent_name: str, rule: AgentRule
) -> TenantAccessPolicy:
    """The policy with `rule` on one agent name; other names keep theirs, in order."""
    rules = dict(policy.agent_rules)
    if rule.runs_in is None:
        rules.pop(agent_name, None)
    else:
        rules[agent_name] = rule
    return _rebuilt(policy, agent_rules=rules)


def runs_only_in(policy: TenantAccessPolicy, channel_id: str) -> tuple[str, ...]:
    """Names whose rule runs them in `channel_id` alone, sorted: its own agents
    while its readers are own."""
    rules = policy.agent_rules.items()
    return tuple(sorted(name for name, rule in rules if set(rule.runs_in or ()) == {channel_id}))


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
    home: str | None = None
    """The channel kept to its own agents (readers own) the place lies in; a
    thread counts as its channel."""
    home_unknown: bool = False
    """A thread whose channel isn't known while some channel's readers are
    own: it may lie in one, so it is treated as one with no own agents."""

    @property
    def answers_external(self) -> bool:
        """Whether people from another organisation (a Teams guest the tenant doesn't
        list as a member) are answered here: only in a channel kept to its own
        agents, and never in a setup conversation or a DM."""
        return self.home is not None

    @property
    def keeps_content(self) -> bool:
        """Whether whatever a turn here writes stays here: posts, direct messages,
        publishing, new agents and routine results (`held_to`)."""
        return self.readers == "own" or self.home_unknown


def _limited(policy: TenantAccessPolicy) -> frozenset[str]:
    return frozenset(i for i, rule in policy.channel_rules.items() if rule.readers != "any")


def any_own_readers(policy: TenantAccessPolicy) -> bool:
    """Whether some channel is kept to its own agents; that work is skipped when none is."""
    return any(rule.readers == "own" for rule in policy.channel_rules.values())


def any_readers_limited(policy: TenantAccessPolicy) -> bool:
    """Whether some channel's readers are inside or own."""
    return bool(_limited(policy))


def any_agent_rules(policy: TenantAccessPolicy) -> bool:
    return bool(policy.agent_rules)


def any_writers_none(policy: TenantAccessPolicy, *, categories_only: bool = False) -> bool:
    """Whether some channel or Discord category (only categories: `categories_only`)
    has writers none."""
    rules = [*policy.category_rules.values()]
    if not categories_only:
        rules += policy.channel_rules.values()
    return any(rule.writers == "none" for rule in rules)


def own_reader_channels(policy: TenantAccessPolicy) -> tuple[str, ...]:
    """Every channel kept to its own agents."""
    return tuple(i for i, rule in policy.channel_rules.items() if rule.readers == "own")


def home_of(
    policy: TenantAccessPolicy, channel_id: str | None, parent_channel_id: str | None = None
) -> str | None:
    """The channel kept to its own agents a place lies in (a thread counts as its
    channel), or None."""
    for candidate in (parent_channel_id, channel_id):
        if candidate is not None and channel_rule(policy, candidate).readers == "own":
            return candidate
    return None


def ruled_agents(policy: TenantAccessPolicy) -> frozenset[str]:
    """Every agent name an agent rule is set on."""
    return frozenset(policy.agent_rules)


def limited_ids(policy: TenantAccessPolicy) -> frozenset[str]:
    """Every id whose readers are inside or own: channels, Discord threads and
    Slack ``channel:ts`` keys."""
    return _limited(policy)


def readers_limited_at(
    policy: TenantAccessPolicy, *, channel_id: str, thread_id: str | None
) -> bool:
    """Whether a turn in `channel_id` (or `thread_id` under it) is somewhere whose
    readers are inside or own."""
    return bool(limiting_ids_at(policy, channel_id=channel_id, thread_id=thread_id))


def limiting_ids_at(
    policy: TenantAccessPolicy, *, channel_id: str, thread_id: str | None
) -> frozenset[str]:
    """Every id limiting who reads a turn there: its channel, its thread, and a
    Slack thread's ``channel:ts``. Recorded, so opening one later leaves the
    others holding."""
    limited = _limited(policy)
    candidates = (
        channel_id,
        thread_id,
        f"{channel_id}:{thread_id}" if thread_id is not None else None,
    )
    return frozenset(c for c in candidates if c is not None and c in limited)


def dm_source_limited(
    policy: TenantAccessPolicy,
    *,
    source_channel_id: str | None,
    source_thread_id: str | None,
    source_thread_keys: Sequence[str] = (),
) -> bool:
    """Whether any recorded source of a DM conversation has limited readers now.

    Fails closed: a conversation without recorded provenance (written before
    it was stored) cannot prove its source open, so any limit in the tenant
    counts. For a Discord thread the parent can't be recovered from the old
    source URL.
    """
    limited = _limited(policy)
    if not limited:
        return False
    if source_channel_id is None:
        return True
    if readers_limited_at(policy, channel_id=source_channel_id, thread_id=source_thread_id):
        return True
    return any(key in limited for key in source_thread_keys)


def _writers_none(
    policy: TenantAccessPolicy,
    *,
    channel_id: str,
    parent_channel_id: str | None,
    category_id: str | None,
    category_unresolved: bool,
) -> bool:
    """Whether the place's channel, its parent, or its Discord category has writers
    none. An unresolved category fails closed while any category has a rule."""
    for candidate in (channel_id, parent_channel_id):
        if candidate is not None and channel_rule(policy, candidate).writers == "none":
            return True
    if category_unresolved and any_writers_none(policy, categories_only=True):
        return True
    rule = policy.category_rules.get(category_id) if category_id is not None else None
    return rule is not None and rule.writers == "none"


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

    `parent_unresolved` marks a thread whose channel isn't known: while some
    channel's readers are own it fails closed (`home_unknown`).
    """
    if channel_id is None and parent_channel_id is None and not parent_unresolved:
        return ChannelPermissions()
    home = home_of(policy, channel_id, parent_channel_id)
    ids = {channel_id, parent_channel_id}
    if channel_id is not None and parent_channel_id is not None:
        ids.add(f"{parent_channel_id}:{channel_id}")
    limited = _limited(policy)
    inside = any(i is not None and i in limited for i in ids)
    no_writers = channel_id is not None and _writers_none(
        policy,
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )
    readers: ChannelReaders = "own" if home is not None else "inside" if inside else "any"
    writers: ChannelWriters = "none" if no_writers else "own" if home is not None else "any"
    return ChannelPermissions(
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        readers=readers,
        writers=writers,
        home=home,
        home_unknown=parent_unresolved and home is None and any_own_readers(policy),
    )


@dataclass(frozen=True)
class AgentPermissions:
    """What one agent may do, from its rule and the rule of its home channel.

    For a member's turn. A server admin may also run an agent with a rule in
    their own DM and hub, where only they see it, and so may a channel admin
    of its home with that channel's own agents. A turn inside a channel kept
    to its own agents keeps whatever agent runs it to that channel (`held_to`).
    """

    runs_in: tuple[frozenset[str], ...] = ()
    """The rule on each of its names; it runs only where each one allows.
    Empty when it has none."""
    home: str | None = None
    """The channel kept to its own agents it is one of the own agents of."""

    def direct_messages(self, origin: ChannelPermissions | None = None) -> Recipients:
        """Whom a call running as it, from a turn at `origin`, may send a direct message."""
        if held_to(self, origin) is not None:
            return "none"
        return "requester" if self.runs_in else "any"

    def publishes(self, origin: ChannelPermissions | None = None) -> bool:
        """Whether such a call may publish (a report, an upload URL, daimon's display
        identity), which anyone holding the link reads, without the requester
        approving it on a card first (`authorize(PUBLISH, approved=...)`)."""
        return held_to(self, origin) is None and not self.runs_in

    def creates_agents(self, origin: ChannelPermissions | None = None) -> bool:
        """Whether such a call may create an agent, which would answer elsewhere."""
        return held_to(self, origin) is None

    @property
    def may_be_copied(self) -> bool:
        """Whether a server admin may fork it; an agent with a rule is never copied."""
        return not self.runs_in

    @property
    def budget_channel(self) -> str | None:
        """The channel whose budget every run is charged to; None charges the
        channel the run is in."""
        return self.home

    def runs_within(self, channel_ids: Collection[str]) -> bool:
        """Whether every rule on it names only `channel_ids`; False with no rule
        or one naming no channel."""
        return bool(self.runs_in) and all(
            rule and rule <= set(channel_ids) for rule in self.runs_in
        )


def agent_permissions(
    policy: TenantAccessPolicy, agent_names: Iterable[str | None]
) -> AgentPermissions:
    """The permissions of the agent carrying `agent_names` (`daimon.core.authz.agent_names`).

    It is one of channel C's own agents when its rules, on every name, name C
    alone and C's readers are own.
    """
    rules = tuple(
        frozenset(policy.agent_rules[name].runs_in or ())
        for name in agent_names
        if name is not None and name in policy.agent_rules
    )
    channels = {channel for rule in rules for channel in rule}
    home = None
    if len(channels) == 1:
        (channel,) = channels
        home = channel if channel_rule(policy, channel).readers == "own" else None
    return AgentPermissions(runs_in=rules, home=home)


def outside_runs_in(
    agent: AgentPermissions, channel_id: str | None, parent_channel_id: str | None = None
) -> bool:
    """Whether some rule on the agent names neither the place nor its channel.

    No channel (a DM, a headless call) is outside every rule.
    """
    place = {channel_id, parent_channel_id}
    return any(not (place & rule) for rule in agent.runs_in)


def run_refusal(
    agent: AgentPermissions, here: ChannelPermissions, *, setup_thread: bool = False
) -> Refusal | None:
    """Why a member's turn may not run the agent here; None when it may.

    Inside every channel its rule names, and inside a channel kept to its own
    agents only as one of them, or in its setup thread. Writers none stops
    the turn, not the agent.
    """
    if outside_runs_in(agent, here.channel_id, here.parent_channel_id):
        return "runs_elsewhere"
    if here.home_unknown:
        return "own_agents_only"
    if not setup_thread and agent.home != here.home:
        return "own_agents_only"
    return None


def runs_at(
    agent: AgentPermissions, here: ChannelPermissions, *, setup_thread: bool = False
) -> bool:
    return run_refusal(agent, here, setup_thread=setup_thread) is None


def held_to(agent: AgentPermissions, origin: ChannelPermissions | None = None) -> str | None:
    """The channel kept to its own agents whose content a call carries: the
    agent's home, else the one its turn runs in."""
    if agent.home is not None:
        return agent.home
    return origin.home if origin is not None else None


def crosses_home(
    agent: AgentPermissions,
    here: ChannelPermissions,
    origin: ChannelPermissions | None = None,
    *,
    setup_origin: bool = False,
) -> bool:
    """Whether acting here would carry content into or out of a channel kept to
    its own agents.

    True for a call held to C (`held_to`) acting outside C, and for any agent
    but C's own acting inside C, unless its turn runs in C's setup thread.
    """
    held = held_to(agent, origin)
    if held != here.home:
        return True
    return held is not None and agent.home != held and not setup_origin


def post_refusal(
    agent: AgentPermissions,
    here: ChannelPermissions,
    origin: ChannelPermissions | None = None,
    *,
    setup_origin: bool = False,
) -> Refusal | None:
    """Why the agent may not post here from a turn at `origin` (None: no turn place).

    The requester's own DM is outside every agent rule, but an agent with one
    posts there; `daimon.core.authz` lets it.
    """
    if here.writers == "none":
        return "writers_none"
    if here.home_unknown or crosses_home(agent, here, origin, setup_origin=setup_origin):
        return "own_agents_only"
    if outside_runs_in(agent, here.channel_id, here.parent_channel_id):
        return "runs_elsewhere"
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
    """Whether the agent is one of the own agents of the channel `here` lies in."""
    return here.home is not None and agent.home == here.home


def memory_writable(agent: AgentPermissions, here: ChannelPermissions) -> bool:
    """Whether a turn here may write the agent's memory, which is read wherever it runs.

    Not where readers are limited, except for a channel's own agents in it
    (`at_home`), whose memory is read nowhere else. A DM's memory follows the
    tenant's `dm_memory_read_only`.
    """
    return here.readers == "any" or at_home(agent, here)


def listed_at(agent: AgentPermissions, inside: str | None) -> bool:
    """Whether a member standing in channel `inside`, kept to its own agents
    (None: outside all of them), sees the agent listed."""
    return agent.home == inside


def readable_from(
    policy: TenantAccessPolicy,
    origin_ids: Collection[str],
    channel_id: str,
    parent_channel_id: str | None = None,
) -> bool:
    """Whether a channel (or a thread under it) is readable from a turn whose ids
    are `origin_ids`: one whose readers are limited only from inside it."""
    limited = _limited(policy)
    if channel_id in limited:
        return channel_id in origin_ids
    if parent_channel_id is not None and parent_channel_id in limited:
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
    policy, and stays inside every id that limited it when it ran, even once
    that id is open again. An unstamped session a thread ran on reads only
    inside that thread while the tenant limits any readers; one no thread ran
    on is headless.
    """
    if channel is not None:
        if not set(seal_ids) <= set(origin_ids):
            return False
        if thread is None:
            return readable_from(policy, origin_ids, channel)
        # A Slack thread takes its own rule as channel_id:thread_ts.
        return readable_from(policy, origin_ids, thread, channel) and readable_from(
            policy, origin_ids, f"{channel}:{thread}", channel
        )
    if legacy_thread_id is None or not any_readers_limited(policy):
        return True
    return legacy_thread_id in origin_ids


def session_homes(
    policy: TenantAccessPolicy, *, channel: str | None, thread: str | None, seal_ids: Iterable[str]
) -> set[str]:
    """The channels kept to their own agents a recorded session lies in: by every
    id that limited it, and by the channel it ran in under the current policy."""
    found = {
        home_of(policy, seal_id, channel if seal_id == thread else seal_id.partition(":")[0])
        for seal_id in seal_ids
    }
    found.add(home_of(policy, channel))
    return {each for each in found if each is not None}


def limited_under(policy: TenantAccessPolicy, channel: str, thread_ids: Iterable[str]) -> bool:
    """Whether `channel` or a thread under it has limited readers: a Slack
    ``channel:ts`` by its key, a Discord thread when `thread_ids` names it."""
    limited = _limited(policy)
    if channel in limited or any(key.startswith(f"{channel}:") for key in limited):
        return True
    return any(thread and thread in limited for thread in thread_ids)


__all__ = [
    "AgentPermissions",
    "AgentRule",
    "ChannelPermissions",
    "ChannelReaders",
    "ChannelRule",
    "ChannelWriters",
    "Recipients",
    "Refusal",
    "RuleRefused",
    "agent_permissions",
    "any_agent_rules",
    "any_own_readers",
    "any_readers_limited",
    "any_writers_none",
    "at_home",
    "channel_permissions",
    "channel_rule",
    "check_channel_rule",
    "crosses_home",
    "dm_source_limited",
    "held_to",
    "home_of",
    "limited_ids",
    "limited_under",
    "limiting_ids_at",
    "listed_at",
    "memory_writable",
    "outside_runs_in",
    "own_reader_channels",
    "post_refusal",
    "posts_at",
    "readable_from",
    "readers_limited_at",
    "ruled_agents",
    "run_refusal",
    "runs_at",
    "runs_only_in",
    "session_homes",
    "session_readable_from",
    "with_agent_rule",
    "with_category_rule",
    "with_channel_rule",
]
