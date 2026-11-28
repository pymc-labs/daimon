"""For each item scheduling, pure."""

from __future__ import annotations

from daimon.core.workflows import engine
from daimon.core.workflows.items import ItemState, item_label, items_next, items_problem
from daimon.core.workflows.spec import ForEachNode


def node(concurrency: int = 2, tolerate: int = 0) -> ForEachNode:
    return ForEachNode.model_validate(
        {
            "kind": "for_each",
            "items": "${{ inputs.x }}",
            "do": {"kind": "agent", "instructions": "Go"},
            "concurrency": concurrency,
            "tolerate_failures": tolerate,
        }
    )


def test_pending_items_start_up_to_the_concurrency() -> None:
    items = [ItemState(0, "running"), ItemState(1, "pending"), ItemState(2, "pending")]
    assert items_next(node(), items).start == (1,), "one slot is free"


def test_a_tolerated_failure_leaves_a_null_output_in_item_order() -> None:
    items = [ItemState(0, "failed", error_class="agent"), ItemState(1, "succeeded", {"a": 1})]
    done = items_next(node(tolerate=1), items).outcome
    assert isinstance(done, engine.Succeeded) and done.output == [None, {"a": 1}], f"{done}"


def test_one_failure_too_many_fails_and_cancels_the_active() -> None:
    items = [ItemState(0, "failed", error_class="agent", error_message="no"), ItemState(1, "ready")]
    nxt = items_next(node(), items)
    assert nxt.cancel == (1,) and isinstance(nxt.outcome, engine.Failed), f"{nxt}"
    assert nxt.outcome.message == "1 of 2 items failed. Item 1: no", nxt.outcome.message


def test_items_must_be_a_short_list_and_get_a_label() -> None:
    assert items_problem("x") == "The items are not a list"
    assert items_problem(list(range(201))) == "For each runs at most 200 items, not 201"
    assert (item_label({"name": "Acme", "x": 1}), item_label(3)) == ("Acme", "3")
