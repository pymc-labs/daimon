"""For each items, pure: which items start next and how the For each step ends.

Each item is a step row of its own (`item_index`, `parent_step_id`) that the steps loop
runs like any step, at most `concurrency` at once. The For each step succeeds with the
item outputs in item order (null for a tolerated failure) once every item ended, and fails
as soon as more items failed than `tolerate_failures` allows, cancelling the rest.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from daimon.core.workflows.engine import (
    ACTIVE,
    ErrorClass,
    Failed,
    Outcome,
    StepStatus,
    Succeeded,
)
from daimon.core.workflows.spec import ForEachNode
from pydantic import JsonValue

MAX_ITEMS: Final = 200
DEFAULT_CONCURRENCY: Final = 1
_LABEL_LIMIT: Final = 80


@dataclass(frozen=True)
class ItemState:
    index: int
    status: StepStatus
    output: JsonValue = None
    error_class: ErrorClass | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ItemsNext:
    #: Pending items to make ready now.
    start: tuple[int, ...] = ()
    #: Active items to cancel: too many failed.
    cancel: tuple[int, ...] = ()
    #: The For each step's outcome once it is settled.
    outcome: Outcome | None = None


def items_problem(value: JsonValue) -> str | None:
    """Why `items` evaluated to something a For each cannot run over."""
    if not isinstance(value, list):
        return "The items are not a list"
    if len(value) > MAX_ITEMS:
        return f"For each runs at most {MAX_ITEMS} items, not {len(value)}"
    return None


def item_label(value: JsonValue) -> str:
    """What the run page calls an item: itself, or its name or title."""
    if isinstance(value, dict):
        named = next((value[k] for k in ("name", "title", "id") if k in value), None)
        text = str(named) if named is not None else ", ".join(map(str, value.values()))
    else:
        text = str(value)
    return text[:_LABEL_LIMIT]


def items_next(node: ForEachNode, items: Sequence[ItemState]) -> ItemsNext:
    failed = [i for i in items if i.status == "failed"]
    if len(failed) > (node.tolerate_failures or 0):
        first = failed[0]
        message = f"{len(failed)} of {len(items)} items failed. Item {first.index + 1}: "
        return ItemsNext(
            cancel=tuple(i.index for i in items if i.status in ACTIVE),
            outcome=Failed(first.error_class or "agent", message + (first.error_message or "")),
        )
    going = sum(1 for i in items if i.status in ACTIVE - {"pending"})
    pending = [i.index for i in items if i.status == "pending"]
    room = max((node.concurrency or DEFAULT_CONCURRENCY) - going, 0)
    if pending or going:
        return ItemsNext(start=tuple(pending[:room]))
    done = sum(1 for i in items if i.status == "succeeded")
    return ItemsNext(
        outcome=Succeeded(
            output=[i.output if i.status == "succeeded" else None for i in items],
            summary=f"{done} of {len(items)} items succeeded",
        )
    )
