"""Scoring: deterministic checks, judge aggregation and interval statistics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from daimon.core.evals.format import (
    Case,
    Check,
    Grader,
    JsonObject,
    RunItem,
    Score,
    ScoreValue,
)
from daimon.core.evals.scoring.checks import check_item, check_value
from daimon.core.evals.scoring.judges import pair_preference
from daimon.core.evals.scoring.stats import cluster_bootstrap
from pydantic import JsonValue

_BUNDLE = Path(__file__).parent / "example_bundle"
_CASE = json.loads((_BUNDLE / "suites/support-basics/cases.jsonl").read_text().splitlines()[0])
_ITEM = json.loads((_BUNDLE / "runs/baseline-a-2026-01/items.jsonl").read_text().splitlines()[0])
Mode = Literal["strict", "unordered", "subset", "superset"]


def _check(**fields: JsonValue) -> Check:
    return Check.model_validate(fields)


@pytest.mark.parametrize(
    ("check", "found", "value", "passes"),
    [
        (_check(op="exact", value="Hi"), True, "Hi", True),
        (_check(op="exact", value="Hi"), True, "hi", False),
        (_check(op="normalized", value=" Hi  there "), True, "hi THERE", True),
        (_check(op="normalized", value="1,000"), True, 1000, True),
        (_check(op="regex", pattern=r"issue #\d+"), True, "opened issue #42", True),
        (_check(op="regex", pattern=r"issue #\d+"), True, "opened an issue", False),
        (_check(op="contains", value="42"), True, "issue 42", True),
        (_check(op="contains", value="x"), True, ["x", "y"], True),
        (_check(op="not_contains", value="error"), True, "all fine", True),
        (_check(op="not_contains", value="error"), False, None, True),
        (_check(op="not_contains", value="error"), True, "an error", False),
        (_check(op="one_of", values=["a", "b"]), True, "b", True),
        (_check(op="one_of", values=["a", "b"]), True, "c", False),
        (_check(op="range", min=1, max=3), True, 2, True),
        (_check(op="range", min=1, max=3), True, 4, False),
        (_check(op="range", min=1), True, "2", False),
        (_check(op="schema", schema={"type": "object", "required": ["n"]}), True, {"n": 1}, True),
        (_check(op="schema", schema={"type": "object", "required": ["n"]}), True, {}, False),
        (_check(op="exists"), True, None, True),
        (_check(op="exists"), False, None, False),
        (_check(op="llm", criterion="Is it polite?"), True, "x", True),
    ],
)
def test_check_value_when_each_op_then_it_decides_as_documented(
    check: Check, found: bool, value: JsonValue, passes: bool
) -> None:
    assert check_value(check, found, value) is passes


def _expected(mode: Mode, tools: list[str]) -> JsonObject:
    calls: list[JsonValue] = [{"match": {"tool": t}} for t in tools]
    return {"tool_calls": {"mode": mode, "calls": calls}}


def _scores(expect: JsonObject) -> dict[str, bool | None]:
    case = Case.model_validate({**_CASE, "expect": expect})
    scores = check_item(
        case, RunItem.model_validate(_ITEM), [], at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    return {s.criterion: s.value.pass_ for s in scores}


# The fixed item made one call, `issue_write`.
@pytest.mark.parametrize(
    ("mode", "tools", "expected"),
    [
        ("strict", ["issue_write"], {"0": True, "extra": True, "order": True}),
        (
            "strict",
            ["search", "issue_write"],
            {"0": False, "1": True, "extra": True, "order": True},
        ),
        ("unordered", ["issue_write", "search"], {"0": True, "1": False, "extra": True}),
        ("unordered", [], {"extra": False}),
        ("subset", ["issue_write", "search"], {"0": True, "1": False}),
        ("subset", [], {}),
        ("superset", ["search"], {"0": True, "extra": False}),
        ("superset", ["issue_write", "search"], {"0": True, "1": True, "extra": True}),
    ],
)
def test_check_item_when_each_match_mode_then_calls_are_judged_by_it(
    mode: Mode, tools: list[str], expected: dict[str, bool]
) -> None:
    found = {
        k.removeprefix("tool_calls/"): v
        for k, v in _scores(_expected(mode, tools)).items()
        if k.startswith("tool_calls/")
    }
    assert found == expected


def _pair(model: str, verdict: Literal["a", "b", "tie"], swapped: bool) -> Score:
    return Score(
        id=f"{model}-{swapped}",
        criterion="preference",
        grader=Grader(kind="llm", model=model),
        value=ScoreValue(preference=verdict),
        swapped=swapped,
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_pair_preference_when_swapped_orders_disagree_then_it_is_a_tie() -> None:
    scores = [_pair("judge-1", "a", False), _pair("judge-1", "b", True)]
    assert pair_preference(scores) == "tie", "a verdict that flips with position is no verdict"
    consistent = [_pair("judge-1", "a", False), _pair("judge-1", "a", True)]
    assert pair_preference(consistent) == "a"


def test_cluster_bootstrap_when_threads_share_cases_then_interval_is_wider_than_by_case() -> None:
    values = [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0]
    by_case = cluster_bootstrap([[v] for v in values], seed=7)
    by_thread = cluster_bootstrap([values[0:3], values[3:6], values[6:9], values[9:12]], seed=7)
    assert by_thread.mean == by_case.mean
    assert by_thread.high - by_thread.low > by_case.high - by_case.low
    assert cluster_bootstrap([values[0:3], values[3:6]], seed=7) == cluster_bootstrap(
        [values[0:3], values[3:6]], seed=7
    ), "a fixed seed gives the same interval"
