"""Pure direct message routing: grants, ordered rules, locks, picks, the default."""

from __future__ import annotations

import pytest
from daimon.core.dm_routing import (
    Candidate,
    DmChoose,
    DmDecision,
    DmGrant,
    DmRefused,
    DmRuleFact,
    DmStart,
    DmUnchecked,
    Person,
    grant_admits,
    resolve,
)

ADA = Person("ada", frozenset({"eng"}))
BLIND = Person("ada", None)


def cands(*names: str, off: tuple[str, ...] = (), blocked: tuple[str, ...] = ()) -> list[Candidate]:
    return [Candidate(n, n not in off, n not in blocked) for n in names]


def decide(
    person: Person = ADA,
    candidates: list[Candidate] | None = None,
    grants: dict[str, DmGrant] | None = None,
    rules: list[DmRuleFact] | None = None,
    default: str | None = None,
    pick: str | None = None,
) -> DmDecision:
    return resolve(
        person,
        candidates if candidates is not None else cands("a", "b"),
        grants or {},
        rules or [],
        default=default,
        pick=pick,
    )


@pytest.mark.parametrize(
    ("grant", "person", "expected"),
    [
        (DmGrant(), ADA, True),
        (DmGrant("listed"), ADA, False),
        (DmGrant("listed", frozenset({"ada"})), BLIND, True),
        (DmGrant("listed", group_ids=frozenset({"eng"})), ADA, True),
        (DmGrant("listed", group_ids=frozenset({"ops"})), ADA, False),
        (DmGrant("listed", group_ids=frozenset({"eng"})), BLIND, None),
        (DmGrant("excluded"), ADA, True),
        (DmGrant("excluded", frozenset({"ada"})), BLIND, False),
        (DmGrant("excluded", group_ids=frozenset({"eng"})), ADA, False),
        (DmGrant("excluded", group_ids=frozenset({"ops"})), ADA, True),
        (DmGrant("excluded", group_ids=frozenset({"ops"})), BLIND, None),
    ],
    ids=[
        "members",
        "listed-empty-admits-nobody",
        "listed-user-needs-no-groups",
        "listed-group-hit",
        "listed-group-miss",
        "listed-group-unknown",
        "excluded-empty-admits-all",
        "excluded-user-needs-no-groups",
        "excluded-group-hit",
        "excluded-group-miss",
        "excluded-group-unknown",
    ],
)
def test_grant_admits_when_mode_and_subjects_given_then_decides(
    grant: DmGrant, person: Person, expected: bool | None
) -> None:
    """Each grant mode against user ids, groups and unknown groups."""
    assert grant_admits(grant, person) is expected, "grant decision differs"


def test_resolve_when_one_agent_admits_then_starts_it_at_once() -> None:
    """A sole agent in A needs no dropdown."""
    d = decide(candidates=cands("a"))

    assert d.route == DmStart("a", "only"), "one agent starts without asking"
    assert d.allowed == ("a",), "allowed lists the agent"


def test_resolve_when_several_admit_and_nothing_chooses_then_offers_the_dropdown() -> None:
    """Several agents and no pick, rule or default: the person chooses."""
    d = decide(candidates=cands("b", "a"))

    assert d.route == DmChoose(("a", "b")), "agents are offered sorted"


def test_resolve_when_nobody_admits_then_refuses() -> None:
    """DMs off, not runnable and a listed grant that omits the person all leave A empty."""
    d = decide(
        candidates=cands("a", "b", "c", off=("a",), blocked=("b",)),
        grants={"c": DmGrant("listed")},
    )

    assert d.route == DmRefused("no_agent"), "empty A refuses"
    assert d.allowed == (), "no agent admits"


def test_resolve_when_pick_rule_and_default_all_apply_then_pick_wins() -> None:
    """Order is pick, rule agent, default."""
    rules = [DmRuleFact("everyone", agent="b")]

    assert decide(rules=rules, default="c", pick="a", candidates=cands("a", "b", "c")).route == (
        DmStart("a", "pick")
    ), "pick first"
    assert decide(rules=rules, default="c", candidates=cands("a", "b", "c")).route == (
        DmStart("b", "rule")
    ), "rule agent next"
    assert decide(default="c", candidates=cands("a", "b", "c")).route == DmStart("c", "default"), (
        "default last"
    )


def test_resolve_when_pick_rule_agent_or_default_left_a_then_falls_through() -> None:
    """A stale pick or a default that no longer admits the person is skipped."""
    grants = {"a": DmGrant("listed")}
    rules = [DmRuleFact("everyone", agent="a")]

    d = decide(grants=grants, rules=rules, default="a", pick="a", candidates=cands("a", "b", "c"))

    assert d.route == DmChoose(("b", "c")), "nothing named is in A, so the person chooses"


def test_resolve_when_rule_is_locked_then_pick_is_ignored() -> None:
    """A lock decides alone, with its agent or the default."""
    rules = [DmRuleFact("user", "ada", agent="b", locked=True)]

    d = decide(rules=rules, pick="a", default="a")

    assert d.route == DmStart("b", "locked"), "lock beats pick"
    assert d.rule == rules[0], "matched rule is reported"
    assert d.may_switch_to("a") is False, "a lock blocks switching"


def test_resolve_when_locked_rule_has_no_agent_then_uses_the_default() -> None:
    """A lock without an agent pins the default."""
    d = decide(rules=[DmRuleFact("everyone", locked=True)], default="b", pick="a")

    assert d.route == DmStart("b", "locked"), "default is the locked target"


@pytest.mark.parametrize("default", [None, "z"], ids=["no-default", "default-not-in-A"])
def test_resolve_when_locked_target_is_unavailable_then_refuses_for_an_admin(
    default: str | None,
) -> None:
    """A lock never falls back to another agent."""
    d = decide(rules=[DmRuleFact("everyone", locked=True)], default=default, pick="a")

    assert d.route == DmRefused("locked_unavailable"), "refused, not rerouted"


def test_resolve_when_locked_agent_excludes_the_person_then_refuses() -> None:
    """The locked agent must admit the person too."""
    d = decide(
        rules=[DmRuleFact("everyone", agent="a", locked=True)],
        grants={"a": DmGrant("excluded", frozenset({"ada"}))},
    )

    assert d.route == DmRefused("locked_unavailable"), "grant still applies under a lock"


def test_resolve_when_first_matching_rule_is_reached_then_later_rules_are_ignored() -> None:
    """Rules match in order and the first match wins."""
    rules = [
        DmRuleFact("user", "other", agent="a"),
        DmRuleFact("group", "eng", agent="b"),
        DmRuleFact("everyone", agent="a", locked=True),
    ]

    d = decide(rules=rules, candidates=cands("a", "b"))

    assert d.route == DmStart("b", "rule"), "group rule beats the later locked everyone rule"
    assert d.may_switch_to("a") is True, "an unlocked rule leaves switching open"


def test_resolve_when_unlocked_rule_agent_is_not_in_a_then_default_applies() -> None:
    """An unlocked rule is a preference; the default follows."""
    d = decide(rules=[DmRuleFact("everyone", agent="z")], default="b", candidates=cands("a", "b"))

    assert d.route == DmStart("b", "default"), "falls to the default"


def test_resolve_when_groups_unknown_and_a_group_rule_precedes_then_fails_closed() -> None:
    """A group rule that cannot be evaluated must not be skipped."""
    d = decide(
        person=BLIND,
        rules=[DmRuleFact("group", "eng", agent="a", locked=True), DmRuleFact("everyone")],
    )

    assert d.route == DmUnchecked(), "unknown groups before a match are unchecked"


def test_resolve_when_a_user_rule_matches_before_a_group_rule_then_groups_are_not_needed() -> None:
    """The first match settles it, so later group rules are never read."""
    d = decide(
        person=BLIND,
        rules=[DmRuleFact("user", "ada", agent="b"), DmRuleFact("group", "eng", agent="a")],
    )

    assert d.route == DmStart("b", "rule"), "user rule matched without groups"


def test_resolve_when_groups_unknown_and_a_grant_needs_them_then_fails_closed() -> None:
    """A group-based grant on a runnable agent cannot be skipped either."""
    d = decide(person=BLIND, grants={"a": DmGrant("listed", group_ids=frozenset({"eng"}))})

    assert d.route == DmUnchecked(), "unknown groups against a group grant are unchecked"


def test_resolve_when_groups_unknown_but_nothing_uses_groups_then_routes_normally() -> None:
    """No group-based grant or rule: unknown groups do not matter."""
    d = decide(person=BLIND, candidates=cands("a"))

    assert d.route == DmStart("a", "only"), "groups unused"


def test_resolve_when_group_grant_is_on_an_agent_that_cannot_run_then_it_is_not_read() -> None:
    """Unknown groups only block on agents that could otherwise take the DM."""
    d = decide(
        person=BLIND,
        candidates=cands("a", "b", off=("a",)),
        grants={"a": DmGrant("listed", group_ids=frozenset({"eng"}))},
    )

    assert d.route == DmStart("b", "only"), "agent a is off, so its grant is moot"


def test_may_switch_to_when_target_not_in_a_then_false() -> None:
    """Switching needs the target in A."""
    d = decide(candidates=cands("a"))

    assert d.may_switch_to("a") is True, "target admits the person"
    assert d.may_switch_to("b") is False, "unknown target does not"
