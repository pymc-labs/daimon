"""Side-by-side comparison of a run's arms, case by case; blind for reviewer-only readers."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from typing import Final

from daimon.core.evals.blind import arm_names, mask_text, neutral_labels
from daimon.core.evals.blind import handle as blind_handle
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
    """The arm's label, or a neutral letter when blind."""
    item_id: str
    """The item's id, or an opaque handle when blind."""
    status: str
    passed: bool | None
    rubric: float | None
    reply: str | None
    scores: list[ScoreLine]


class CompareRow(_Out):
    case_id: str
    title: str
    repeat: int
    columns: list[CompareColumn]


class Comparison(_Out):
    run_id: str
    blind: bool
    rows: list[CompareRow]


def _reply(case: Case | None, item: RunItem, names: frozenset[str] | None) -> str | None:
    texts = [t for _, t in bot_messages(case, item)] if case else []
    text = "\n\n".join(t for t in texts if t) or None
    return mask_text(text, names) if text and names is not None else text


def _lines(scores: Sequence[Score], names: frozenset[str] | None) -> list[ScoreLine]:
    replaced = {s.supersedes for s in scores if s.supersedes}
    found: list[ScoreLine] = []
    for s in scores:
        if s.id in replaced:
            continue
        why = s.rationale if s.grader.kind not in _NO_RATIONALE else None
        found.append(
            ScoreLine(
                criterion=s.criterion, grader=s.grader.kind, passed=s.value.pass_,
                score=s.value.score, unknown=bool(s.value.unknown),
                rationale=mask_text(why, names) if why and names is not None else why,
            )
        )  # fmt: skip
    return found


def build_comparison(
    run: Run,
    items: Sequence[RunItem],
    scores: Sequence[Score],
    cases: Sequence[Case],
    *,
    blind: bool,
    arm_ids: Sequence[str] | None = None,
    salt: str | None = None,
) -> Comparison:
    """One row per case and repeat, one column per arm that has an item for it.

    Blind: columns carry neutral letters and opaque item handles, replies and rationales have
    model names masked, and each row's column order is a fixed random shuffle of the salt and
    the row, so position never tells the arm.
    """
    salt = salt or run.id
    names = arm_names(run) if blind else None
    labels = neutral_labels([a.id for a in run.arms], salt, run.blind_labels)
    arm_label = {a.id: a.label for a in run.arms}
    case_by_id = {c.id: c for c in cases}
    judges = {j.id: j for j in run.judges or []}
    by_item: dict[str, list[Score]] = defaultdict(list)
    for s in scores:
        if s.item_id:
            by_item[s.item_id].append(s)
    groups: dict[tuple[str, int], list[RunItem]] = defaultdict(list)
    for item in items:
        if item.status in ("done", "error") and (arm_ids is None or item.arm_id in arm_ids):
            groups[(item.case.id, item.repeat)].append(item)
    rows: list[CompareRow] = []
    for (case_id, repeat), group in sorted(groups.items()):
        group.sort(key=lambda i: i.arm_id)
        if blind:
            seed = int(sha256(f"{salt}|{case_id}|{repeat}".encode()).hexdigest(), 16)
            random.Random(seed).shuffle(group)
        case = case_by_id.get(case_id)
        columns: list[CompareColumn] = []
        for item in group:
            own = by_item.get(item.id, [])
            result = outcome(item, case, own, judges)
            label = labels.get(item.arm_id, "arm") if blind else arm_label.get(item.arm_id, "")
            columns.append(
                CompareColumn(
                    label=label,
                    item_id=blind_handle(salt, item.id) if blind else item.id,
                    status=item.status,
                    passed=result.passed,
                    rubric=result.rubric,
                    reply=_reply(case, item, names),
                    scores=_lines(own, names),
                )  # fmt: skip
            )
        rows.append(
            CompareRow(
                case_id=case_id,
                title=case.title if case else case_id,
                repeat=repeat,
                columns=columns,
            )
        )
    return Comparison(run_id=run.id if not blind else "run", blind=blind, rows=rows)
