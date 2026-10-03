"""The permissions model: rule writes, and views that match `authorize`."""

from __future__ import annotations

import functools
import itertools
import random
from collections.abc import Iterator

import pytest
from daimon.core.access_policy import OPEN_ACCESS_POLICY, OPEN_RULE, TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.permissions import (
    AgentRule,
    ChannelRule,
    RuleRefused,
    agent_permissions,
    channel_permissions,
    channel_rule,
    held_to,
    memory_writable,
    own_reader_channels,
    posts_at,
    readers_limited_at,
    runs_at,
    runs_only_in,
    with_agent_rule,
    with_category_rule,
    with_channel_rule,
)
from daimon.core.rule_views import RuleViewer

_MEMBER = Subject(platform_user_id="u1")
_ADMIN = Subject(is_admin=True, platform_user_id="u1")
# Agent rules on x; y takes fewer to keep the product small. "t" is a thread under "a".
_RUNS_X: tuple[tuple[str, ...] | None, ...] = (None, (), ("a",), ("b",), ("a", "b"), ("t",))
_RUNS_Y: tuple[tuple[str, ...] | None, ...] = (None, ("a",), ("t",))
_AGENTS: tuple[tuple[str | None, ...], ...] = (("x",), ("y",), ("x", "y"), ("x", None), ("z",))
# A channel, a Discord thread under "a", a Slack thread key under "a" (its key
# shape is not checked here).
_READ_PLACES = (Place(channel_id="a"), Place(channel_id="b"), Place("t", "a"), Place("a:ts", "a"))
# Where a turn runs: channels, and Discord threads under "a" and "b".
_TURN_PLACES = (Place(channel_id="a"), Place(channel_id="b"), Place("t", "a"), Place("u", "b"))
_INSIDE = ChannelRule(readers="inside")
_NO_WRITERS = ChannelRule(writers="none")
_OWN = ChannelRule(readers="own", writers="own")
# Every rule a channel can hold.
_RULES = (
    OPEN_RULE,
    _NO_WRITERS,
    _INSIDE,
    ChannelRule(readers="inside", writers="none"),
    _OWN,
    ChannelRule(readers="own", writers="none"),
)
# A thread's own rule: none, readers inside by Discord id or Slack key, or writers none.
_THREAD_RULES: tuple[tuple[str, ChannelRule] | None, ...] = (
    None,
    ("t", _INSIDE),
    ("a:ts", _INSIDE),
    ("t", _NO_WRITERS),
)


def _policy(
    rule_a: ChannelRule,
    rule_b: ChannelRule,
    thread_rule: tuple[str, ChannelRule] | None,
    category: bool,
    runs_x: tuple[str, ...] | None,
    runs_y: tuple[str, ...] | None,
) -> TenantAccessPolicy:
    channels = {"a": rule_a, "b": rule_b, **dict([thread_rule] if thread_rule else [])}
    runs = {"x": runs_x, "y": runs_y}
    return TenantAccessPolicy(
        channel_rules=channels,
        category_rules={"cat": _NO_WRITERS} if category else {},
        agent_rules={name: AgentRule(runs_in=r) for name, r in runs.items() if r is not None},
    )


def _policies() -> Iterator[TenantAccessPolicy]:
    """Every policy over channels a and b, a thread rule, a category and rules on x and y."""
    for combo in itertools.product(_RULES, _RULES, _THREAD_RULES, (False, True), _RUNS_X, _RUNS_Y):
        yield _policy(*combo)


@functools.cache
def _sampled_policies() -> tuple[TenantAccessPolicy, ...]:
    """What the slower checks run on, to keep CI fast: every pair of channel rules
    with every rule on x (thread rule, category and y cycled), plus a fixed random
    sample of the rest."""
    cycled = (
        _policy(a, b, _THREAD_RULES[i % 4], (i // 4) % 2 == 1, runs_x, _RUNS_Y[i % 3])
        for i, (a, b, runs_x) in enumerate(itertools.product(_RULES, _RULES, _RUNS_X))
    )
    return (*cycled, *random.Random(406).sample(list(_policies()), 100))


def test_category_takes_only_writers_none() -> None:
    """A category limits no readers."""
    with pytest.raises(RuleRefused, match="only takes writers none"):
        with_category_rule(OPEN_ACCESS_POLICY, "cat", _INSIDE)
    closed = with_category_rule(OPEN_ACCESS_POLICY, "cat", _NO_WRITERS)
    assert closed.category_rules == {"cat": _NO_WRITERS}, "category closed"
    assert with_category_rule(closed, "cat", OPEN_RULE) == OPEN_ACCESS_POLICY, "category reopened"


def test_with_agent_rule_sets_and_clears_one_name() -> None:
    """No channels clears the rule; other names keep theirs."""
    policy = TenantAccessPolicy(
        agent_rules={"x": AgentRule(runs_in=("a",)), "y": AgentRule(runs_in=("b",))}
    )
    nowhere = with_agent_rule(policy, "x", AgentRule(runs_in=()))
    assert nowhere.agent_rules["x"].runs_in == (), "x now runs nowhere"
    assert list(nowhere.agent_rules) == ["x", "y"], "x keeps its place"
    cleared = with_agent_rule(nowhere, "x", AgentRule())
    assert list(cleared.agent_rules) == ["y"], "x cleared, y kept"


def test_with_channel_rule_leaves_other_ids_alone() -> None:
    """Changing one channel keeps every other channel's rule."""
    policy = TenantAccessPolicy(
        channel_rules={"a": _OWN, "b": ChannelRule(readers="inside", writers="none")}
    )
    changed = with_channel_rule(policy, "a", OPEN_RULE)
    assert changed.channel_rules == {"b": channel_rule(policy, "b")}, "only b keeps a rule"


@pytest.mark.parametrize("rule", [_NO_WRITERS, _OWN], ids=str)
def test_slack_thread_key_takes_only_readers_inside(rule: ChannelRule) -> None:
    """Who writes in a Slack thread, and own agents, follow its channel."""
    with pytest.raises(RuleRefused, match="only takes readers inside"):
        with_channel_rule(OPEN_ACCESS_POLICY, "C01AB:1700000000.000200", rule)
    inside = with_channel_rule(OPEN_ACCESS_POLICY, "C01AB:1700000000.000200", _INSIDE)
    assert inside.channel_rules == {"C01AB:1700000000.000200": _INSIDE}, "a Slack thread"


def test_teams_channel_id_takes_any_rule() -> None:
    """A Teams channel id holds ":" but is a channel, not a Slack thread key."""
    channel = "19:abc123@thread.tacv2"
    policy = with_channel_rule(OPEN_ACCESS_POLICY, channel, _OWN)
    assert channel_rule(policy, channel) == _OWN, "Teams channel kept to its own agents"


# What each rule limits, as docs/permissions.md lists it, for a channel "c"
# and a thread under it: (agent "f" without a rule, own agent "o" for readers own).
_LIMITS: dict[ChannelRule, dict[str, object]] = {
    OPEN_RULE: {"keeps_content": False, "runs": (True, None), "memory": (True, None)},
    _NO_WRITERS: {"keeps_content": False, "runs": (True, None), "memory": (True, None)},
    _INSIDE: {"keeps_content": False, "runs": (True, None), "memory": (False, None)},
    _OWN: {"keeps_content": True, "runs": (False, True), "memory": (False, True)},
}


@pytest.mark.parametrize("rule", list(_LIMITS), ids=str)
def test_each_rule_limits_what_the_docs_say(rule: ChannelRule) -> None:
    """Whether content stays, which agents run and whose memory is writable.

    Writers none stops the turn, not the agent."""
    policy = with_channel_rule(OPEN_ACCESS_POLICY, "c", rule)
    agents = [agent_permissions(policy, ("f",))]
    if rule.readers == "own":
        policy = with_agent_rule(policy, "o", AgentRule(runs_in=("c",)))
        agents = [agent_permissions(policy, ("f",)), agent_permissions(policy, ("o",))]
    for place in (("c", None), ("t", "c")):
        here = channel_permissions(policy, channel_id=place[0], parent_channel_id=place[1])
        runs = [runs_at(agent, here) for agent in agents]
        memory = [memory_writable(agent, here) for agent in agents]
        got = {
            "keeps_content": here.keeps_content,
            "runs": (*runs, None) if len(agents) == 1 else tuple(runs),
            "memory": (*memory, None) if len(agents) == 1 else tuple(memory),
        }
        assert got == _LIMITS[rule], f"{rule} at {place}: {got}"


def test_unknown_parent_fails_closed_only_while_some_channel_has_own_readers() -> None:
    """A thread whose channel is unknown may lie in a channel kept to its own agents."""
    kept = TenantAccessPolicy(channel_rules={"c": _OWN})
    for policy, closed in ((OPEN_ACCESS_POLICY, False), (kept, True)):
        for thread in ("t", None):
            here = channel_permissions(policy, channel_id=thread, parent_unresolved=True)
            assert here.keeps_content == closed, f"content kept at {thread} under {policy}"
            free = agent_permissions(policy, ("f",))
            assert runs_at(free, here) != closed, f"free agent runs at {thread} under {policy}"


def test_channel_permissions_match_authorize() -> None:
    """A channel's readers and writers are what `authorize` enforces there."""
    for policy, place, category in itertools.product(
        _sampled_policies(), _READ_PLACES, (None, "cat")
    ):
        assert place.channel_id is not None, "every read place names a channel"
        view = channel_permissions(
            policy,
            channel_id=place.channel_id,
            parent_channel_id=place.parent_channel_id,
            category_id=category,
        )
        at = Place(place.channel_id, place.parent_channel_id, category_id=category)
        started = authorize(policy, subject=_ADMIN, action=Action.START_TURN, place=at)
        assert (view.writers == "none") == (started.reason == "writers_none"), (
            f"writers at {at} under {policy}"
        )
        read = authorize(policy, subject=Subject(), action=Action.READ_CHANNEL, place=place)
        assert (view.readers != "any") == (read.reason == "not_a_reader"), (
            f"readers at {place} under {policy}"
        )
        stranger = authorize(
            policy, subject=_MEMBER, action=Action.RUN_AGENT, agent=AgentRef.of("z"), place=place
        )
        assert (view.readers == "own") == (stranger.reason == "own_agents_only"), (
            f"own readers at {place} under {policy}"
        )
        channel = view.home
        for name in runs_only_in(policy, channel) if channel is not None else ():
            own = authorize(
                policy, subject=_MEMBER, action=Action.RUN_AGENT, agent=AgentRef.of(name), place=at
            )
            assert own, f"own agent {name} refused at {at} under {policy}"


def test_slack_thread_turn_reads_its_key_rule() -> None:
    """A Slack turn's place (ts under its channel) reads the ``channel:ts`` rule."""
    for policy in _policies():
        view = channel_permissions(policy, channel_id="ts", parent_channel_id="a")
        limited = readers_limited_at(policy, channel_id="a", thread_id="ts")
        assert (view.readers != "any") == limited, f"Slack thread readers under {policy}"


def test_agent_permissions_match_authorize() -> None:
    """Where an agent runs, posts, messages, publishes and is listed is what `authorize`
    allows a member."""
    for policy, names in itertools.product(_sampled_policies(), _AGENTS):
        view = agent_permissions(policy, names)
        agent = AgentRef.of(*names)
        for place in _TURN_PLACES:
            assert place.channel_id is not None, "every turn place names a channel"
            here = channel_permissions(
                policy, channel_id=place.channel_id, parent_channel_id=place.parent_channel_id
            )
            ran = authorize(
                policy, subject=_MEMBER, action=Action.RUN_AGENT, agent=agent, place=place
            )
            assert runs_at(view, here) == bool(ran), f"{names} run at {place} under {policy}"
            posted = authorize(
                policy, subject=_MEMBER, action=Action.POST, agent=agent, place=place
            )
            assert posts_at(view, here) == bool(posted), f"{names} post at {place} under {policy}"
        own_dm = authorize(
            policy, subject=_MEMBER, action=Action.POST, agent=agent, place=Place(own_dm=True)
        )
        assert bool(own_dm) == (view.home is None), f"{names} own DM under {policy}"
        to_requester, to_other = (
            authorize(
                policy,
                subject=_MEMBER,
                action=Action.DIRECT_MESSAGE,
                agent=agent,
                recipient_id=recipient,
            )
            for recipient in ("u1", "u2")
        )
        expected = {"any": (True, True), "requester": (True, False), "none": (False, False)}
        assert (bool(to_requester), bool(to_other)) == expected[view.direct_messages()], (
            f"{names} direct messages under {policy}"
        )
        published = authorize(policy, subject=_MEMBER, action=Action.PUBLISH, agent=agent)
        assert bool(published) == view.publishes(), f"{names} publish under {policy}"
        created = authorize(policy, subject=_MEMBER, action=Action.CREATE_AGENT, agent=agent)
        assert bool(created) == view.creates_agents(), f"{names} create under {policy}"
        forked = authorize(
            policy, subject=_ADMIN, action=Action.FORK, surface=Surface.HUB, agent=agent
        )
        assert bool(forked) == view.may_be_copied, f"{names} fork under {policy}"
        for inside in (None, *own_reader_channels(policy)):
            listed = RuleViewer(policy, inside_channel_id=inside).sees_names(names)
            assert listed == (view.home == inside), f"{names} listed from {inside} under {policy}"


def test_a_turn_inside_an_own_readers_channel_keeps_its_content() -> None:
    """From a turn whose place keeps content, whatever agent runs posts, messages,
    publishes and creates agents exactly as the agent's permissions from there say."""
    for policy, names in itertools.product(_sampled_policies(), _AGENTS):
        view = agent_permissions(policy, names)
        agent = AgentRef.of(*names)
        for origin in _TURN_PLACES:
            assert origin.channel_id is not None, "every turn place names a channel"
            at = channel_permissions(
                policy, channel_id=origin.channel_id, parent_channel_id=origin.parent_channel_id
            )
            held = held_to(view, at) is not None
            assert held == (at.keeps_content or view.home is not None), (
                f"{names} held from {origin} under {policy}"
            )
            allowed = {
                Action.DIRECT_MESSAGE: view.direct_messages(at) != "none",
                Action.CREATE_AGENT: view.creates_agents(at),
                Action.PUBLISH: view.publishes(at),
            }
            for action, expected in allowed.items():
                decided = authorize(
                    policy,
                    subject=_MEMBER,
                    action=action,
                    agent=agent,
                    origin=origin,
                    recipient_id=_MEMBER.platform_user_id,
                )
                assert bool(decided) == expected, f"{names} {action} from {origin} under {policy}"
                assert not (held and decided), f"{names} {action} held from {origin}"
            for place in _TURN_PLACES:
                assert place.channel_id is not None, "every turn place names a channel"
                here = channel_permissions(
                    policy, channel_id=place.channel_id, parent_channel_id=place.parent_channel_id
                )
                posted = authorize(
                    policy,
                    subject=_MEMBER,
                    action=Action.POST,
                    agent=agent,
                    place=place,
                    origin=origin,
                )
                assert posts_at(view, here, at) == bool(posted), (
                    f"{names} post at {place} from {origin} under {policy}"
                )
