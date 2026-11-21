"""A workflow's runs as a board and a calendar: the card a run shows, and the stage it is in.

The card is rendered at each settle and stored on the run; the stage is never stored, it is
read from the run's current step, so a spec edit moves no stored data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime

from daimon.core.workflows.engine import RunFacts, StepView, expression_context
from daimon.core.workflows.expressions import ExpressionError, render
from daimon.core.workflows.spec import Spec
from pydantic import JsonValue

DONE = "Done"
FAILED = "Failed"
#: The column of a running run when the spec names no stages.
ACTIVE = "In progress"
#: Runs that need a person, as the badge shows them.
ATTENTION = ("approval", "question", "unknown", "blocked")
_LIVE = ("running", "waiting", "unknown")
_ENDED_BAD = ("failed", "timed_out", "cancelled")
_FIELD_LIMIT = 300
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def render_card(
    spec: Spec, steps: Sequence[StepView], facts: RunFacts, *, now: datetime
) -> dict[str, JsonValue] | None:
    """The spec's card fields for this run; a field that fails to render is null."""
    if spec.card is None:
        return None
    context = expression_context(spec, steps, "", facts)
    card: dict[str, JsonValue] = {}
    for name, template in spec.card.fields.items():
        try:
            value = render(template, context, now=now)
        except ExpressionError:  # one bad field must not hide the others
            value = None
        if isinstance(value, dict | list):
            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        card[name] = value[:_FIELD_LIMIT] if isinstance(value, str) else value
    return card


def stages_of(spec: Spec) -> list[str]:
    """The stage labels in the order the nodes are declared."""
    return list(dict.fromkeys(n.stage for n in spec.nodes.values() if n.stage))


def stage_of(spec: Spec, steps: Sequence[tuple[str, str]], status: str) -> str:
    """Where a run is: Done or Failed once ended, else its current step's stage.

    `steps` are (node, status) of the steps that started, oldest first. A current step
    without a stage keeps the stage of the latest step that had one.
    """
    if status == "succeeded":
        return DONE
    if status in _ENDED_BAD:
        return FAILED
    live = [n for n, s in steps if s in _LIVE]
    unknown = [n for n, s in steps if s == "unknown"]
    current = (unknown or live)[-1:]
    for node_id in [*current, *reversed([n for n, _ in steps])]:
        node = spec.nodes.get(node_id)
        if node is not None and node.stage:
            return node.stage
    return (stages_of(spec) or [ACTIVE])[0]


def needs_attention(status: str, waiting_reason: str | None) -> bool:
    return status == "waiting" and waiting_reason in ATTENTION


def valid_month(month: str) -> bool:
    return _MONTH.fullmatch(month) is not None
