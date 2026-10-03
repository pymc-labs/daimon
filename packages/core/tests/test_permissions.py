"""The permissions model: rules round-trip the access policy, views match `authorize`."""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy, is_sealed_source
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.channel_isolation import IsolationViewer
from daimon.core.permissions import (
    CHANNEL_PRESETS,
    AgentRule,
    ChannelRule,
    agent_permissions,
    agent_rules,
    category_rules,
    channel_permissions,
    channel_rule,
    channel_rules,
    posts_at,
    preset_of,
    runs_at,
    with_agent_rule,
    with_category_rule,
    with_channel_rule,
)
from pydantic import ValidationError

_MEMBER = Subject(platform_user_id="u1")
_ADMIN = Subject(is_admin=True, platform_user_id="u1")
# Pins on x; y takes fewer to keep the product small. "t" is a thread under "a".
_PINS_X: tuple[tuple[str, ...] | None, ...] = (None, (), ("a",), ("b",), ("a", "b"), ("t",))
_PINS_Y: tuple[tuple[str, ...] | None, ...] = (None, ("a",), ("t",))
_AGENTS: tuple[tuple[str | None, ...], ...] = (("x",), ("y",), ("x", "y"), ("x", None), ("z",))
# A channel, a Discord thread under "a", a Slack thread key under "a".
_READ_PLACES = (Place(channel_id="a"), Place(channel_id="b"), Place("t", "a"), Place("a:ts", "a"))
# Where a turn runs: channels, and Discord threads under "a" and "b".
_TURN_PLACES = (Place(channel_id="a"), Place(channel_id="b"), Place("t", "a"), Place("u", "b"))
# A thread's own rule: none, sealed by Discord id or Slack key, or protected.
_THREAD_RULES: tuple[tuple[str, str] | None, ...] = (
    None,
    ("sealed", "t"),
    ("sealed", "a:ts"),
    ("protected", "t"),
)


def _channel_states() -> Iterator[tuple[bool, bool, bool]]:
    """(protected, sealed, isolated) for one channel; isolation needs the seal."""
    for protected, sealed in itertools.product((False, True), repeat=2):
        yield protected, sealed, False
        if sealed:
            yield protected, sealed, True


def _policies() -> Iterator[TenantAccessPolicy]:
    """Every policy over channels a and b, a thread rule, a category and pins on x and y."""
    for state_a, state_b, thread_rule, category, pin_x, pin_y in itertools.product(
        _channel_states(), _channel_states(), _THREAD_RULES, (False, True), _PINS_X, _PINS_Y
    ):
        states = {"a": state_a, "b": state_b}
        pins = {name: pin for name, pin in (("x", pin_x), ("y", pin_y)) if pin is not None}
        sealed_thread = thread_rule[1] if thread_rule and thread_rule[0] == "sealed" else None
        protected_thread = thread_rule[1] if thread_rule and thread_rule[0] == "protected" else None
        yield TenantAccessPolicy(
            protected_channel_ids=(
                *(c for c, s in states.items() if s[0]),
                *((protected_thread,) if protected_thread else ()),
            ),
            protected_category_ids=("cat",) if category else (),
            sealed_channel_ids=(
                *(c for c, s in states.items() if s[1]),
                *((sealed_thread,) if sealed_thread else ()),
            ),
            isolated_channel_ids=tuple(c for c, s in states.items() if s[2]),
            agent_channel_pins=pins,
        )


def _normalized(policy: TenantAccessPolicy) -> dict[str, object]:
    dumped = policy.model_dump()
    return {
        key: frozenset(value) if isinstance(value, tuple) else value
        for key, value in dumped.items()
    }


def test_presets_name_the_legacy_terms() -> None:
    """Each preset writes exactly the legacy lists its name stands for."""
    expected = {
        "open": TenantAccessPolicy(),
        "protected": TenantAccessPolicy(protected_channel_ids=("c",)),
        "sealed": TenantAccessPolicy(sealed_channel_ids=("c",)),
        "confidential": TenantAccessPolicy(sealed_channel_ids=("c",), isolated_channel_ids=("c",)),
    }
    for name, rule in CHANNEL_PRESETS.items():
        written = with_channel_rule(OPEN_ACCESS_POLICY, "c", rule)
        assert written == expected[name], f"preset {name} wrote {written}"
        assert preset_of(channel_rule(written, "c")) == name, f"preset {name} reads back"


def test_preset_of_a_sealed_protected_channel_is_none() -> None:
    """A mix of presets is no single preset."""
    rule = ChannelRule(readers="inside", writers="nobody")
    assert preset_of(rule) is None, "sealed and protected together is a mix"


@pytest.mark.parametrize(
    ("readers", "writers"),
    [("anyone", "own_agents"), ("inside", "own_agents"), ("own_agents", "any_agent")],
)
def test_channel_rule_keeps_own_agents_on_both_sides(readers: str, writers: str) -> None:
    """Own agents can't be a channel's only writers without being its only readers."""
    with pytest.raises(ValidationError):
        ChannelRule.model_validate({"readers": readers, "writers": writers})


def test_category_takes_only_open_or_protected() -> None:
    """A category has no seal or isolation to set."""
    with pytest.raises(ValueError, match="only be open or protected"):
        with_category_rule(OPEN_ACCESS_POLICY, "cat", CHANNEL_PRESETS["sealed"])
    protected = with_category_rule(OPEN_ACCESS_POLICY, "cat", CHANNEL_PRESETS["protected"])
    assert protected.protected_category_ids == ("cat",), "category protected"
    reopened = with_category_rule(protected, "cat", CHANNEL_PRESETS["open"])
    assert reopened == OPEN_ACCESS_POLICY, "category reopened"


def test_with_agent_rule_pins_and_unpins_one_name() -> None:
    """None removes the pin; other names keep theirs."""
    policy = TenantAccessPolicy(agent_channel_pins={"x": ("a",), "y": ("b",)})
    pinned = with_agent_rule(policy, "x", AgentRule(runs_in=()))
    assert pinned.agent_channel_pins == {"x": (), "y": ("b",)}, "x now runs nowhere"
    unpinned = with_agent_rule(pinned, "x", AgentRule())
    assert unpinned.agent_channel_pins == {"y": ("b",)}, "x unpinned, y kept"


def test_with_channel_rule_leaves_other_ids_alone() -> None:
    """Changing one channel keeps every other channel's rule."""
    policy = TenantAccessPolicy(
        protected_channel_ids=("b",), sealed_channel_ids=("a", "b"), isolated_channel_ids=("a",)
    )
    changed = with_channel_rule(policy, "a", CHANNEL_PRESETS["open"])
    assert channel_rule(changed, "b") == channel_rule(policy, "b"), "b keeps its rule"
    assert channel_rules(changed) == {"b": ChannelRule(readers="inside", writers="nobody")}, (
        "only b keeps a rule"
    )


@pytest.mark.parametrize(
    "rule", [CHANNEL_PRESETS["protected"], CHANNEL_PRESETS["confidential"]], ids=str
)
def test_slack_thread_key_takes_only_a_seal(rule: ChannelRule) -> None:
    """Nothing protects or isolates a Slack thread by its key, so no rule says so."""
    with pytest.raises(ValueError, match="can only be sealed"):
        with_channel_rule(OPEN_ACCESS_POLICY, "a:ts", rule)
    sealed = with_channel_rule(OPEN_ACCESS_POLICY, "a:ts", CHANNEL_PRESETS["sealed"])
    assert sealed.sealed_channel_ids == ("a:ts",), "a Slack thread may be sealed"


def test_rules_rebuild_every_policy() -> None:
    """Reading a policy as rules and writing them back loses nothing but list order."""
    for policy in _policies():
        rebuilt = OPEN_ACCESS_POLICY
        for channel_id, rule in channel_rules(policy).items():
            rebuilt = with_channel_rule(rebuilt, channel_id, rule)
        for category_id, rule in category_rules(policy).items():
            rebuilt = with_category_rule(rebuilt, category_id, rule)
        for name, rule in agent_rules(policy).items():
            rebuilt = with_agent_rule(rebuilt, name, rule)
        assert _normalized(rebuilt) == _normalized(policy), f"lost a rule of {policy}"


def test_channel_permissions_match_authorize() -> None:
    """A channel's readers and writers are what `authorize` enforces there."""
    for policy, place, category in itertools.product(_policies(), _READ_PLACES, (None, "cat")):
        assert place.channel_id is not None, "every read place names a channel"
        view = channel_permissions(
            policy,
            channel_id=place.channel_id,
            parent_channel_id=place.parent_channel_id,
            category_id=category,
        )
        at = Place(place.channel_id, place.parent_channel_id, category_id=category)
        started = authorize(policy, subject=_ADMIN, action=Action.START_TURN, place=at)
        assert (view.writers == "nobody") == (started.reason == "channel_protected"), (
            f"protection at {at} under {policy}"
        )
        read = authorize(policy, subject=Subject(), action=Action.READ_CHANNEL, place=place)
        assert (view.readers != "anyone") == (read.reason == "sealed"), (
            f"seal at {place} under {policy}"
        )
        stranger = authorize(
            policy, subject=_MEMBER, action=Action.RUN_AGENT, agent=AgentRef.of("z"), place=place
        )
        assert (view.readers == "own_agents") == (stranger.reason == "channel_isolated"), (
            f"isolation at {place} under {policy}"
        )
        for name in view.own_agent_names:
            own = authorize(
                policy, subject=_MEMBER, action=Action.RUN_AGENT, agent=AgentRef.of(name), place=at
            )
            assert own, f"own agent {name} refused at {at} under {policy}"


def test_slack_thread_turn_is_inside_its_key_seal() -> None:
    """A Slack turn's place (ts under its channel) reads the ``channel:ts`` seal."""
    for policy in _policies():
        view = channel_permissions(policy, channel_id="ts", parent_channel_id="a")
        sealed = is_sealed_source(policy, channel_id="a", thread_id="ts")
        assert (view.readers != "anyone") == sealed, f"Slack thread seal under {policy}"


def test_agent_permissions_match_authorize() -> None:
    """Where an agent runs, posts, messages and is listed is what `authorize` allows a member."""
    for policy, names in itertools.product(_policies(), _AGENTS):
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
        assert bool(own_dm) == view.posts_to_requester_dm, f"{names} own DM under {policy}"
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
        expected = {"anyone": (True, True), "requester": (True, False), "nobody": (False, False)}
        assert (bool(to_requester), bool(to_other)) == expected[view.direct_messages], (
            f"{names} direct messages under {policy}"
        )
        forked = authorize(
            policy, subject=_ADMIN, action=Action.FORK, surface=Surface.HUB, agent=agent
        )
        assert bool(forked) == view.may_be_copied, f"{names} fork under {policy}"
        own = view.listed_in == "its_confidential_channel"
        assert own == (view.confidential_channel is not None), f"{names} listing under {policy}"
        for inside in (None, *policy.isolated_channel_ids):
            listed = IsolationViewer(policy, inside_channel_id=inside).sees_names(names)
            assert listed == (view.confidential_channel == inside), (
                f"{names} listed from {inside} under {policy}"
            )
