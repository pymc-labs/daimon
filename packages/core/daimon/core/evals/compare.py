"""Side-by-side comparison of a run's arms, case by case; blind for reviewers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Final

from daimon.core.evals.blind import arm_labels, arm_names, mask_text, opaque_id, roll
from daimon.core.evals.format import Case, Run, RunItem, Score
from daimon.core.evals.scoring.checks import bot_messages
from daimon.core.evals.scoring.summary import outcome
from pydantic import BaseModel, ConfigDict

_NO_RATIONALE: Final = frozenset({"human"})


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScoreLine(_Out):
    criterion: str
    grader: str
    passed: bool | None
    score: float | None
    unknown: bool
    rationale: str | None


class CompareColumn(_Out):
    label: str
    """The arm's label, or its neutral label when blind."""
    item_id: str
    """The item's id, or its opaque id (what the item view takes) when blind."""
    status: str
    passed: bool | None
    rubric: float | None
    reply: str | None
    scores: list[ScoreLine]


class CompareRow(_Out):
    case_id: str
    title: str
    repeat: int
    """0 when blind: a repeat index tells live arms from recorded ones."""
    columns: list[CompareColumn]


class Comparison(_Out):
    run_id: str
    blind: bool
    rows: list[CompareRow]


def _reply(case: Case | None, item: RunItem, names: frozenset[str] | None) -> str | None:
    texts = [t for _, t in bot_messages(case, item)] if case else []
    text = "\n\n".join(t for t in texts if t) or None
    return mask_text(text, names) if text and names is not None else text


def _lines(scores: Sequence[Score]) -> list[ScoreLine]:
    replaced = {s.supersedes for s in scores if s.supersedes}
    return [
        ScoreLine(
            criterion=s.criterion, grader=s.grader.kind, passed=s.value.pass_,
            score=s.value.score, unknown=bool(s.value.unknown),
            rationale=s.rationale if s.grader.kind not in _NO_RATIONALE else None,
        )
        for s in scores
        if s.id not in replaced
    ]  # fmt: skip


def build_comparison(
    run: Run,
    items: Sequence[RunItem],
    scores: Sequence[Score],
    cases: Sequence[Case],
    *,
    blind: bool,
    secret: str,
    arms: Sequence[str] | None = None,
) -> Comparison:
    """One row per case and repeat, one column per arm that has an item for it. `arms` picks
    arms by id, or by neutral label when blind.

    Blind: columns carry the arms' neutral labels and opaque ids for this view, replies have
    model names masked, scores are left out, only finished items of repeats every chosen arm
    finished are shown (an error or a missing repeat tells live arms from recorded ones), and
    each row's column order is a shuffle keyed by `secret`, so position never tells the arm.
    """
    names = arm_names(run) if blind else None
    labels = arm_labels(run, secret)
    chosen = {a.id for a in run.arms}
    if arms is not None:
        chosen = {a for a in chosen if (labels[a] if blind else a) in arms}
    arm_label = {a.id: a.label for a in run.arms}
    case_by_id = {c.id: c for c in cases}
    judges = {j.id: j for j in run.judges or []}
    by_item: dict[str, list[Score]] = defaultdict(list)
    for s in scores:
        if s.item_id:
            by_item[s.item_id].append(s)
    groups: dict[tuple[str, int], list[RunItem]] = defaultdict(list)
    for item in items:
        if item.arm_id in chosen and item.status in (("done",) if blind else ("done", "error")):
            groups[(item.case.id, item.repeat)].append(item)
    rows: list[CompareRow] = []
    for (case_id, repeat), group in sorted(groups.items()):
        if blind and {i.arm_id for i in group} != chosen:
            continue
        group.sort(key=lambda i: i.arm_id)
        if blind:
            roll(secret, "compare", run.id, case_id, str(repeat)).shuffle(group)
        case = case_by_id.get(case_id)
        columns: list[CompareColumn] = []
        for item in group:
            own = by_item.get(item.id, [])
            result = outcome(item, case, own, judges)
            columns.append(
                CompareColumn(
                    label=labels[item.arm_id] if blind else arm_label.get(item.arm_id, ""),
                    item_id=opaque_id(secret, run.id, "compare", item.id) if blind else item.id,
                    status=item.status,
                    passed=None if blind else result.passed,
                    rubric=None if blind else result.rubric,
                    reply=_reply(case, item, names),
                    scores=[] if blind else _lines(own),
                )
            )  # fmt: skip
        rows.append(
            CompareRow(
                case_id=case_id, title=case.title if case else case_id,
                repeat=0 if blind else repeat, columns=columns,
            )
        )  # fmt: skip
    return Comparison(run_id=run.id, blind=blind, rows=rows)
