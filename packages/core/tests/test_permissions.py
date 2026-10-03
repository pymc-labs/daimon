"""The permissions model: rules round-trip the access policy, views match `authorize`."""

from __future__ import annotations

import functools
import itertools
import random
from collections.abc import Iterator

import pytest
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy, is_sealed_source
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.channel_isolation import IsolationViewer
from daimon.core.permissions import (
    CHANNEL_PRESETS,
    AgentRule,
    ChannelPreset,
    ChannelRule,
    agent_permissions,
    agent_rules,
    category_rules,
    channel_permissions,
    channel_rule,
    channel_rules,
    held_to,
    memory_writable,
    pinned_alone,
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
# A channel, a Discord thread under "a", a Slack thread key under "a" (its key
# shape is not checked here).
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


def _policy(
    state_a: tuple[bool, bool, bool],
    state_b: tuple[bool, bool, bool],
    thread_rule: tuple[str, str] | None,
    category: bool,
    pin_x: tuple[str, ...] | None,
    pin_y: tuple[str, ...] | None,
) -> TenantAccessPolicy:
    states = {"a": state_a, "b": state_b}
    pins = {name: pin for name, pin in (("x", pin_x), ("y", pin_y)) if pin is not None}
    sealed_thread = thread_rule[1] if thread_rule and thread_rule[0] == "sealed" else None
    protected_thread = thread_rule[1] if thread_rule and thread_rule[0] == "protected" else None
    return TenantAccessPolicy(
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


def _policies() -> Iterator[TenantAccessPolicy]:
    """Every policy over channels a and b, a thread rule, a category and pins on x and y."""
    for combo in itertools.product(
        _channel_states(), _channel_states(), _THREAD_RULES, (False, True), _PINS_X, _PINS_Y
    ):
        yield _policy(*combo)


@functools.cache
def _sampled_policies() -> tuple[TenantAccessPolicy, ...]:
    """What the slower checks run on, to keep CI fast: every pair of channel states
    with every pin on x (thread rule, category and y cycled), plus a fixed random
    sample of the rest."""
    states = list(_channel_states())
    cycled = (
        _policy(a, b, _THREAD_RULES[i % 4], (i // 4) % 2 == 1, pin_x, _PINS_Y[i % 3])
        for i, (a, b, pin_x) in enumerate(itertools.product(states, states, _PINS_X))
    )
    return (*cycled, *random.Random(406).sample(list(_policies()), 100))


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
    rule = ChannelRule(readers="inside", writers="none")
    assert preset_of(rule) is None, "sealed and protected together is a mix"


@pytest.mark.parametrize(
    ("readers", "writers"), [("any", "own"), ("inside", "own"), ("own", "any")]
)
def test_channel_rule_keeps_own_on_both_sides(readers: str, writers: str) -> None:
    """Own agents write only a channel they alone read, and read one only they or nothing write."""
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
    assert list(pinned.agent_channel_pins) == ["x", "y"], "x keeps its place"
    unpinned = with_agent_rule(pinned, "x", AgentRule())
    assert unpinned.agent_channel_pins == {"y": ("b",)}, "x unpinned, y kept"


def test_with_channel_rule_leaves_other_ids_alone() -> None:
    """Changing one channel keeps every other channel's rule."""
    policy = TenantAccessPolicy(
        protected_channel_ids=("b",), sealed_channel_ids=("a", "b"), isolated_channel_ids=("a",)
    )
    changed = with_channel_rule(policy, "a", CHANNEL_PRESETS["open"])
    assert channel_rule(changed, "b") == channel_rule(policy, "b"), "b keeps its rule"
    assert channel_rules(changed) == {"b": ChannelRule(readers="inside", writers="none")}, (
        "only b keeps a rule"
    )


@pytest.mark.parametrize(
    "rule", [CHANNEL_PRESETS["protected"], CHANNEL_PRESETS["confidential"]], ids=str
)
def test_slack_thread_key_takes_only_a_seal(rule: ChannelRule) -> None:
    """Nothing protects or isolates a Slack thread by its key, so no rule says so."""
    with pytest.raises(ValueError, match="can only be sealed"):
        with_channel_rule(OPEN_ACCESS_POLICY, "C01AB:1700000000.000200", rule)
    sealed = with_channel_rule(
        OPEN_ACCESS_POLICY, "C01AB:1700000000.000200", CHANNEL_PRESETS["sealed"]
    )
    assert sealed.sealed_channel_ids == ("C01AB:1700000000.000200",), "a Slack thread is sealed"


def test_teams_channel_id_takes_any_rule() -> None:
    """A Teams channel id holds ":" but is a channel, not a Slack thread key."""
    channel = "19:abc123@thread.tacv2"
    policy = with_channel_rule(OPEN_ACCESS_POLICY, channel, CHANNEL_PRESETS["confidential"])
    assert preset_of(channel_rule(policy, channel)) == "confidential", "Teams channel confidential"


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


# What each preset limits, as docs/permissions.md lists it, for a channel "c"
# and a thread under it: (free agent "f", own agent "o" when confidential).
_LIMITS: dict[ChannelPreset, dict[str, object]] = {
    "open": {"keeps_content": False, "runs": (True, None), "memory": (True, None)},
    "protected": {"keeps_content": False, "runs": (True, None), "memory": (True, None)},
    "sealed": {"keeps_content": False, "runs": (True, None), "memory": (False, None)},
    "confidential": {"keeps_content": True, "runs": (False, True), "memory": (False, True)},
}


@pytest.mark.parametrize("preset", list(_LIMITS))
def test_each_preset_limits_what_the_docs_say(preset: ChannelPreset) -> None:
    """Whether content stays, which agents run and whose memory is writable.

    Protection stops the turn (`writers`), not the agent."""
    policy = with_channel_rule(OPEN_ACCESS_POLICY, "c", CHANNEL_PRESETS[preset])
    agents = [agent_permissions(policy, ("f",))]
    if preset == "confidential":
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
        assert got == _LIMITS[preset], f"{preset} at {place}: {got}"


def test_unknown_parent_fails_closed_only_while_something_is_confidential() -> None:
    """A thread whose channel is unknown may lie in a confidential channel."""
    confidential = TenantAccessPolicy(sealed_channel_ids=("c",), isolated_channel_ids=("c",))
    for policy, closed in ((OPEN_ACCESS_POLICY, False), (confidential, True)):
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
        assert (view.writers == "none") == (started.reason == "channel_protected"), (
            f"protection at {at} under {policy}"
        )
        read = authorize(policy, subject=Subject(), action=Action.READ_CHANNEL, place=place)
        assert (view.readers != "any") == (read.reason == "sealed"), (
            f"seal at {place} under {policy}"
        )
        stranger = authorize(
            policy, subject=_MEMBER, action=Action.RUN_AGENT, agent=AgentRef.of("z"), place=place
        )
        assert (view.readers == "own") == (stranger.reason == "channel_isolated"), (
            f"isolation at {place} under {policy}"
        )
        channel = view.confidential_channel
        for name in pinned_alone(policy, channel) if channel is not None else ():
            own = authorize(
                policy, subject=_MEMBER, action=Action.RUN_AGENT, agent=AgentRef.of(name), place=at
            )
            assert own, f"own agent {name} refused at {at} under {policy}"


def test_slack_thread_turn_is_inside_its_key_seal() -> None:
    """A Slack turn's place (ts under its channel) reads the ``channel:ts`` seal."""
    for policy in _policies():
        view = channel_permissions(policy, channel_id="ts", parent_channel_id="a")
        sealed = is_sealed_source(policy, channel_id="a", thread_id="ts")
        assert (view.readers != "any") == sealed, f"Slack thread seal under {policy}"


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
        assert bool(own_dm) == (view.kind != "own"), f"{names} own DM under {policy}"
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
        for inside in (None, *policy.isolated_channel_ids):
            listed = IsolationViewer(policy, inside_channel_id=inside).sees_names(names)
            assert listed == (view.own_channel == inside), (
                f"{names} listed from {inside} under {policy}"
            )


def test_a_turn_inside_a_confidential_channel_keeps_its_content() -> None:
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
            assert held == (at.keeps_content or view.own_channel is not None), (
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
