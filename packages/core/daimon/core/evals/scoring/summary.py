"""A run's results: pass rates, intervals, paired comparisons, breakdowns, judge agreement."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from daimon.core.evals.format import Case, JudgeConfig, Run, RunItem, Score, StatsDefaults
from daimon.core.evals.scoring import stats
from daimon.core.evals.scoring.checks import is_hard
from daimon.core.evals.scoring.judges import pair_preference, vote
from pydantic import BaseModel, ConfigDict

KAPPA_WARN: Final = 0.4
_KINDS: Final = ("human", "agent", "llm", "code")
_META: Final = frozenset({"overall", "preference"})
BASELINE_NOTE: Final = (
    "A baseline holds one recorded answer per case, so its variance is unknown; "
    "intervals count only the other arm's repeats."
)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Rate(_Out):
    items: int
    passed: int
    pass_rate: float | None


class ArmSummary(_Out):
    arm_id: str
    label: str
    items: int
    scored: int
    errors: int
    passed: int
    pass_rate: float | None
    rubric_score: float | None
    cost_usd: float
    pass_at_k: dict[int, float]
    pass_pow_k: dict[int, float]
    breakdown: dict[str, dict[str, Rate]]


class PairSummary(_Out):
    candidate: str
    reference: str
    metric: str
    cases: int
    difference: float | None
    low: float | None
    high: float | None
    margin: float
    non_inferior: bool | None
    only_candidate: int
    only_reference: int
    mcnemar_p: float | None


class PreferenceSummary(_Out):
    arm_a: str
    arm_b: str
    a_wins: int
    b_wins: int
    ties: int
    sign_test_p: float | None


class Calibration(_Out):
    judge: str
    n: int
    agreement: float | None
    kappa: float | None
    flagged: bool


class RunSummary(_Out):
    run_id: str
    arms: list[ArmSummary]
    comparisons: list[PairSummary]
    preferences: list[PreferenceSummary]
    calibration: list[Calibration]
    judge_cost_usd: float
    notes: list[str]


@dataclass(frozen=True)
class Outcome:
    item: RunItem
    passed: bool | None
    """None: nothing decisive scored it yet."""
    rubric: float | None


def _num(value: float | None) -> float | None:
    return None if value is None or math.isnan(value) else value


def _decide(scores: Sequence[Score], judges: Mapping[str, JudgeConfig]) -> bool | float | None:
    """One criterion's value from every score on it: the most trusted grader kind that has an
    answer decides; its graders vote (judge panels by their config) or average."""
    for kind in _KINDS:
        own = [s for s in scores if s.grader.kind == kind and not s.value.unknown]
        if not own:
            continue
        marks = [s.value.score for s in own if s.value.score is not None]
        if marks:
            return sum(marks) / len(marks)
        flags = [s.value.pass_ for s in own if s.value.pass_ is not None]
        configs = [judges[j] for s in own if (j := s.grader.judge_id) is not None and j in judges]
        mode = configs[0].aggregate if len({c.id for c in configs}) == 1 else "majority"
        if flags:
            return vote(flags, mode)
    return None


def _live(scores: Sequence[Score]) -> list[Score]:
    replaced = {s.supersedes for s in scores if s.supersedes}
    return [s for s in scores if s.id not in replaced]


def _share(value: bool | float, scale_min: float, scale_max: float) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return (value - scale_min) / (scale_max - scale_min) if scale_max > scale_min else 0.0


def outcome(
    item: RunItem, case: Case | None, scores: Sequence[Score], judges: Mapping[str, JudgeConfig]
) -> Outcome:
    by_criterion: dict[str, list[Score]] = defaultdict(list)
    for score in scores:
        by_criterion[score.criterion].append(score)
    decided = {c: _decide(group, judges) for c, group in by_criterion.items()}
    if case is None:
        return Outcome(item, None, None)
    rule = case.expect.pass_rule
    every = rule is None or rule.checks == "all"
    rubric_ids = {r.id: r for r in case.expect.rubric or []}
    failed = False
    answered = False
    for criterion, value in decided.items():
        if value is None or criterion in _META or criterion in rubric_ids:
            continue
        answered = True
        if isinstance(value, bool) and not value and (every or is_hard(case, criterion)):
            failed = True
    weight = got = 0.0
    hard_missed = False
    minimum = rule.rubric_min if rule else 0.7
    for rubric_id, rubric in rubric_ids.items():
        value = decided.get(rubric_id)
        if value is None:
            continue
        answered = True
        low, high = (rubric.scale.min, rubric.scale.max) if rubric.scale else (0, 1)
        share = _share(value, low, high)
        weight += rubric.weight
        got += rubric.weight * share
        hard_missed = hard_missed or (
            rubric.hard and share < (1.0 if rubric.kind == "binary" else minimum)
        )
    rubric = got / weight if weight else None
    overall = decided.get("overall")
    if isinstance(overall, bool):
        return Outcome(item, overall, rubric)
    if not answered:
        return Outcome(item, None, rubric)
    return Outcome(
        item, not failed and not hard_missed and (rubric is None or rubric >= minimum), rubric
    )


def _rate(outcomes: Sequence[Outcome]) -> Rate:
    scored = [o for o in outcomes if o.passed is not None]
    passed = sum(bool(o.passed) for o in scored)
    return Rate(
        items=len(scored), passed=passed, pass_rate=passed / len(scored) if scored else None
    )


def _buckets(case: Case, item: RunItem) -> dict[str, list[str]]:
    return {
        "fidelity": [case.fidelity.level],
        "platform": [case.surface.platform],
        "tag": case.tags or ["untagged"],
        "substitution": [
            "substituted" if item.enclosure and item.enclosure.substitutions else "none"
        ],
    }


def _arm_summary(
    label: str, arm_id: str, outcomes: Sequence[Outcome], cases: Mapping[str, Case]
) -> ArmSummary:
    scored = [o for o in outcomes if o.passed is not None]
    rubrics = [o.rubric for o in outcomes if o.rubric is not None]
    grouped: dict[str, dict[str, list[Outcome]]] = defaultdict(lambda: defaultdict(list))
    per_case: dict[str, list[bool]] = defaultdict(list)
    for o in scored:
        case = cases.get(o.item.case.id)
        per_case[o.item.case.id].append(bool(o.passed))
        for dim, names in (_buckets(case, o.item) if case else {}).items():
            for name in names:
                grouped[dim][name].append(o)
    repeats = max((len(v) for v in per_case.values()), default=0)
    at_k: dict[int, float] = {}
    pow_k: dict[int, float] = {}
    for k in range(2, min(repeats, 5) + 1):
        rows = [(len(v), sum(v)) for v in per_case.values() if len(v) >= k]
        at_k[k] = sum(stats.pass_at_k(n, c, k) for n, c in rows) / len(rows)
        pow_k[k] = sum(stats.pass_pow_k(n, c, k) for n, c in rows) / len(rows)
    rate = _rate(outcomes)
    return ArmSummary(
        arm_id=arm_id, label=label, items=len(outcomes), scored=len(scored),
        errors=sum(o.item.status == "error" for o in outcomes), passed=rate.passed,
        pass_rate=rate.pass_rate, rubric_score=sum(rubrics) / len(rubrics) if rubrics else None,
        cost_usd=sum(o.item.usage.cost_usd or 0 for o in outcomes if o.item.usage),
        pass_at_k=at_k, pass_pow_k=pow_k,
        breakdown={d: {n: _rate(v) for n, v in sorted(g.items())} for d, g in grouped.items()},
    )  # fmt: skip


def _case_values(outcomes: Sequence[Outcome], metric: str) -> dict[str, float]:
    found: dict[str, list[float]] = defaultdict(list)
    for o in outcomes:
        if metric == "rubric_score":
            if o.rubric is not None:
                found[o.item.case.id].append(o.rubric)
        elif o.passed is not None:
            found[o.item.case.id].append(1.0 if o.passed else 0.0)
    return {case: sum(v) / len(v) for case, v in found.items()}


def _pairs(arms: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """(candidate, reference): each live arm against each baseline, else against the first arm."""
    baselines = [a for a, kind in arms if kind == "baseline"]
    if baselines:
        return [(a, b) for a, kind in arms if kind == "live" for b in baselines]
    return [(a, arms[0][0]) for a, _ in arms[1:]]


def _compare(
    candidate: str, reference: str, outcomes: Mapping[str, Sequence[Outcome]],
    cases: Mapping[str, Case], rule: StatsDefaults,
) -> PairSummary:  # fmt: skip
    metric = "rubric_score" if rule.primary == "rubric_score" else "pass_rate"
    ours = _case_values(outcomes[candidate], metric)
    theirs = _case_values(outcomes[reference], metric)
    shared = sorted(ours.keys() & theirs.keys())
    deltas = {c: ours[c] - theirs[c] for c in shared}
    keys = (
        {c: cases[c].source_thread for c in shared if c in cases}
        if rule.cluster_by == "source_thread"
        else {}
    )
    ni = stats.non_inferiority(
        stats.group_by_cluster(deltas, keys), margin=rule.non_inferiority_margin
    )
    only_a = only_b = 0
    if metric == "pass_rate":
        for c in shared:
            only_a += ours[c] > 0.5 >= theirs[c]
            only_b += theirs[c] > 0.5 >= ours[c]
    return PairSummary(
        candidate=candidate, reference=reference, metric=metric, cases=len(shared),
        difference=_num(ni.difference) if shared else None, low=_num(ni.low) if shared else None,
        high=_num(ni.high) if shared else None, margin=ni.margin,
        non_inferior=ni.verdict if shared else None, only_candidate=only_a, only_reference=only_b,
        mcnemar_p=stats.mcnemar(only_a, only_b) if metric == "pass_rate" and shared else None,
    )  # fmt: skip


def _preferences(scores: Sequence[Score], arm_of: Mapping[str, str]) -> list[PreferenceSummary]:
    by_pair: dict[tuple[str, str], list[Score]] = defaultdict(list)
    for s in scores:
        if s.pair is not None:
            by_pair[(s.pair.a, s.pair.b)].append(s)
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for (a, b), group in by_pair.items():
        verdict = pair_preference(group)
        if verdict is None or a not in arm_of or b not in arm_of:
            continue
        key = (arm_of[a], arm_of[b])
        tally[key][{"a": 0, "b": 1, "tie": 2}[verdict]] += 1
    return [
        PreferenceSummary(
            arm_a=a, arm_b=b, a_wins=w[0], b_wins=w[1], ties=w[2],
            sign_test_p=stats.binomial_two_sided(w[0], w[0] + w[1]) if w[0] + w[1] else None,
        )
        for (a, b), w in sorted(tally.items())
    ]  # fmt: skip


def calibration(scores: Sequence[Score], *, warn: float = KAPPA_WARN) -> list[Calibration]:
    """Agreement of each judge model with people on the yes/no criteria both scored."""
    humans: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for s in scores:
        if s.grader.kind == "human" and s.item_id and s.value.pass_ is not None:
            humans[(s.item_id, s.criterion)].append(s.value.pass_)
    pairs: dict[str, tuple[list[bool], list[bool]]] = defaultdict(lambda: ([], []))
    for s in scores:
        if s.grader.kind != "llm" or not s.item_id or s.value.pass_ is None:
            continue
        consensus = vote(humans.get((s.item_id, s.criterion), []), "majority")
        if consensus is None:
            continue
        pairs[s.grader.model or "judge"][0].append(consensus)
        pairs[s.grader.model or "judge"][1].append(s.value.pass_)
    found: list[Calibration] = []
    for judge, (people, model) in sorted(pairs.items()):
        kappa = _num(stats.cohen_kappa(people, model))
        agree = sum(a == b for a, b in zip(people, model, strict=True)) / len(people)
        found.append(
            Calibration(
                judge=judge,
                n=len(people),
                agreement=agree,
                kappa=kappa,
                flagged=kappa is None or kappa < warn,
            )
        )
    return found


def summarize(
    run: Run,
    items: Sequence[RunItem],
    scores: Sequence[Score],
    cases: Sequence[Case],
    *,
    rule: StatsDefaults | None = None,
    kappa_warn: float = KAPPA_WARN,
) -> RunSummary:
    """Results of a run: per arm, paired against baselines, by breakdown, with judge agreement.

    `items` are the run's items (scores only name them); `rule` is the suite's stats defaults.
    """
    rule = rule or StatsDefaults()
    case_by_id = {c.id: c for c in cases}
    judges = {j.id: j for j in run.judges or []}
    live = _live(scores)
    by_item: dict[str, list[Score]] = defaultdict(list)
    for s in live:
        if s.item_id:
            by_item[s.item_id].append(s)
    outcomes: dict[str, list[Outcome]] = {a.id: [] for a in run.arms}
    for item in items:
        if item.status in ("done", "error") and item.arm_id in outcomes:
            outcomes[item.arm_id].append(
                outcome(item, case_by_id.get(item.case.id), by_item.get(item.id, []), judges)
            )
    arms = [_arm_summary(a.label, a.id, outcomes[a.id], case_by_id) for a in run.arms]
    kinds = [(a.id, a.kind) for a in run.arms]
    comparisons = [_compare(c, r, outcomes, case_by_id, rule) for c, r in _pairs(kinds)]
    arm_of = {i.id: i.arm_id for i in items}
    notes = [BASELINE_NOTE] if any(kind == "baseline" for _, kind in kinds) else []
    pending = sum(o.passed is None for group in outcomes.values() for o in group)
    if pending:
        notes.append(f"{pending} finished items have no decisive score yet.")
    return RunSummary(
        run_id=run.id, arms=arms, comparisons=comparisons,
        preferences=_preferences(live, arm_of), calibration=calibration(live, warn=kappa_warn),
        judge_cost_usd=sum(s.cost_usd or 0 for s in live if s.grader.kind == "llm"), notes=notes,
    )  # fmt: skip
