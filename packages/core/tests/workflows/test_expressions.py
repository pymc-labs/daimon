from __future__ import annotations

from datetime import UTC, datetime

import pytest
from daimon.core.workflows.expressions import (
    MAX_DEPTH,
    MAX_SOURCE,
    ExpressionError,
    Ref,
    check_expression,
    evaluate,
    expression_refs,
    render,
)
from pydantic import JsonValue

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
CONTEXT: dict[str, JsonValue] = {
    "inputs": {"n": 2, "tags": ["a", "b"]},
    "steps": {"draft": {"output": {"body": "Hi"}}},
}


def test_refs_follow_dots_and_literal_indexes_and_mark_has_guards() -> None:
    refs = expression_refs(
        "has(steps.draft.output) ? steps['draft'].output.body : inputs.tags[0] + x.y"
    )

    assert refs == [
        Ref(("steps", "draft", "output"), guarded=True),
        Ref(("steps", "draft", "output", "body")),
        Ref(("inputs", "tags")),
    ], "paths rooted at namespaces only, guards marked"


def test_render_keeps_the_type_of_a_lone_expression_and_joins_text() -> None:
    assert render("${{ inputs.n * 2 }}", CONTEXT, now=NOW) == 4, "a lone int stays an int"
    assert render({"to": ["${{ inputs.tags }}"]}, CONTEXT, now=NOW) == {"to": [["a", "b"]]}, "walks"
    assert render("n=${{ inputs.n }} ${{ inputs.tags }}", CONTEXT, now=NOW) == 'n=2 ["a","b"]', (
        "text around expressions makes a string"
    )
    assert render("${{ now() }}", CONTEXT, now=NOW) == "2026-11-20T12:00:00Z", "now() is injected"
    assert render("${{ lowerAscii(steps.draft.output.body) }}", CONTEXT, now=NOW) == "hi", "funcs"


def test_last_takes_the_final_items_of_a_list() -> None:
    assert render("${{ last(inputs.tags, 1) }}", CONTEXT, now=NOW) == ["b"], "the final one"
    assert render("${{ last(inputs.tags, 5) }}", CONTEXT, now=NOW) == ["a", "b"], "all when short"
    assert render("${{ last(inputs.tags, 0) }}", CONTEXT, now=NOW) == [], "none"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("inputs.n +", "does not parse"),
        ("1" + " + 1" * MAX_SOURCE, "longer than"),
        ("(" * MAX_DEPTH + "1" + ")" * MAX_DEPTH, "nested too deeply"),
    ],
    ids=["parse", "length", "depth"],
)
def test_static_bounds_refuse_before_evaluating(source: str, message: str) -> None:
    with pytest.raises(ExpressionError, match=message):
        check_expression(source)


def test_step_budget_covers_macros() -> None:
    source = "[1,2,3,4,5,6,7,8,9,10].all(a, [1,2,3,4,5,6,7,8,9,10].all(b, a + b > 0))"

    assert evaluate(source, {}, now=NOW) is True, "fits the default budget"
    with pytest.raises(ExpressionError, match="too costly"):
        evaluate(source, {}, now=NOW, steps=100)


def test_runtime_errors_become_expression_errors() -> None:
    with pytest.raises(ExpressionError, match="failed"):
        evaluate("inputs.n / 0", CONTEXT, now=NOW)


def test_a_context_nested_past_the_stack_is_an_expression_error() -> None:
    deep: JsonValue = 1
    for _ in range(3000):
        deep = {"a": deep}

    with pytest.raises(ExpressionError, match="nested too deeply"):
        evaluate("inputs.n", {"inputs": deep}, now=NOW)
