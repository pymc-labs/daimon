"""Pure direct message routing: which agent a person's private message reaches.

No I/O. The caller gathers the facts (grants, rules, the default, the person's
pick, and per agent whether DMs are on and the person may run it) and gets one
decision. Groups the platform could not confirm are `None`, never empty: any
check that needs them fails closed as `DmUnchecked`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

GrantMode = Literal["members", "listed", "excluded"]
RuleSubject = Literal["user", "group", "everyone"]


@dataclass(frozen=True)
class DmGrant:
    """Who may DM one agent: every member, only those listed, or all but those listed."""

    mode: GrantMode = "members"
    user_ids: frozenset[str] = frozenset()
    group_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DmRuleFact:
    """One routing rule. `agent` None means the workspace default."""

    subject: RuleSubject
    subject_id: str | None = None
    agent: str | None = None
    locked: bool = False


@dataclass(frozen=True)
class Person:
    """The sender. `groups` None means the platform could not say."""

    user_id: str
    groups: frozenset[str] | None


@dataclass(frozen=True)
class Candidate:
    """One agent: DMs on at the agent tier, and whether the person may run it in a DM."""

    name: str
    dm_on: bool
    may_run: bool


@dataclass(frozen=True)
class DmStart:
    """Start with `agent`. `reason` says which input chose it."""

    agent: str
    reason: Literal["locked", "pick", "rule", "default", "only"]


@dataclass(frozen=True)
class DmChoose:
    """Several agents fit and nothing chose: the person picks."""

    agents: tuple[str, ...]


@dataclass(frozen=True)
class DmRefused:
    """No agent can take this person: none admits them, or a lock names one that does not."""

    reason: Literal["no_agent", "locked_unavailable"]


@dataclass(frozen=True)
class DmUnchecked:
    """Group membership was unknown where it mattered; try again."""


Route = DmStart | DmChoose | DmRefused | DmUnchecked


@dataclass(frozen=True)
class DmDecision:
    """The route, the agents that admit the person, and the rule that matched."""

    route: Route
    allowed: tuple[str, ...] = ()
    rule: DmRuleFact | None = None

    def may_switch_to(self, agent: str) -> bool:
        """A person may move to `agent` when it admits them and no lock applies."""
        locked = self.rule is not None and self.rule.locked
        return agent in self.allowed and not locked


def grant_admits(grant: DmGrant, person: Person) -> bool | None:
    """Whether the grant admits the person; None when it hinges on unknown groups."""
    if grant.mode == "members":
        return True
    if person.user_id in grant.user_ids:
        return grant.mode == "listed"
    if not grant.group_ids:
        return grant.mode == "excluded"
    if person.groups is None:
        return None
    hit = bool(person.groups & grant.group_ids)
    return hit if grant.mode == "listed" else not hit


def _rule_matches(rule: DmRuleFact, person: Person) -> bool | None:
    if rule.subject == "everyone":
        return True
    if rule.subject == "user":
        return person.user_id == rule.subject_id
    if person.groups is None:
        return None
    return rule.subject_id in person.groups


def matching_rule(rules: Sequence[DmRuleFact], person: Person) -> tuple[bool, DmRuleFact | None]:
    """The first rule that applies, as (known, rule). Unknown groups before a match: not known."""
    for rule in rules:
        hit = _rule_matches(rule, person)
        if hit is None:
            return False, None
        if hit:
            return True, rule
    return True, None


def resolve(
    person: Person,
    candidates: Sequence[Candidate],
    grants: Mapping[str, DmGrant],
    rules: Sequence[DmRuleFact],
    *,
    default: str | None,
    pick: str | None,
) -> DmDecision:
    """Choose the agent for a person's DM.

    Agents that admit the person (A) are those with DMs on, runnable by them in
    a DM, and whose grant admits them. A locked matching rule decides alone;
    otherwise the first of pick, rule agent and default that is in A wins, then
    a sole agent in A, else the person chooses.
    """
    allowed: list[str] = []
    for c in candidates:
        if not (c.dm_on and c.may_run):
            continue
        admits = grant_admits(grants.get(c.name, DmGrant()), person)
        if admits is None:
            return DmDecision(DmUnchecked())
        if admits:
            allowed.append(c.name)
    known, rule = matching_rule(rules, person)
    if not known:
        return DmDecision(DmUnchecked())
    names = tuple(allowed)
    if rule is not None and rule.locked:
        target = rule.agent or default
        if target is not None and target in names:
            return DmDecision(DmStart(target, "locked"), names, rule)
        return DmDecision(DmRefused("locked_unavailable"), names, rule)
    rule_agent = None if rule is None else rule.agent
    if pick is not None and pick in names:
        return DmDecision(DmStart(pick, "pick"), names, rule)
    if rule_agent is not None and rule_agent in names:
        return DmDecision(DmStart(rule_agent, "rule"), names, rule)
    if default is not None and default in names:
        return DmDecision(DmStart(default, "default"), names, rule)
    if len(names) == 1:
        return DmDecision(DmStart(names[0], "only"), names, rule)
    if names:
        return DmDecision(DmChoose(tuple(sorted(names))), names, rule)
    return DmDecision(DmRefused("no_agent"), names, rule)
