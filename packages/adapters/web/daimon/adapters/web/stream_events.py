"""A turn's state as the web chat shows it, and the events that move one view to the next.

Pure: the lifecycle keeps the clock and the previous view.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from daimon.adapters.web.stream import Event
from daimon.core.stores.domain import WebStep
from daimon.core.turn.state import ContentBlock, ToolUseBlock, extract_final_response
from daimon.core.turn.status_lines import tool_label


@dataclass(frozen=True)
class ReplyView:
    text: str = ""
    steps: tuple[WebStep, ...] = ()


_STATUS: dict[str, Literal["running", "done", "error"]] = {
    "pending": "running",
    "complete": "done",
    "failed": "error",
}


def step_of(call: ToolUseBlock, ms: int | None) -> WebStep:
    label, detail = tool_label(call)
    status = _STATUS[call.status]
    return WebStep(
        id=call.id,
        label=label,
        detail=detail,
        status=status,
        ms=None if status == "running" else ms,
    )


def view_of(
    content: Sequence[ContentBlock],
    *,
    durations_ms: Mapping[str, int],
    hidden: Collection[str] = (),
    shown_text: str = "",
) -> ReplyView:
    """The reply in `content`. `hidden` are steps an earlier segment showed finished;
    `shown_text` is the answer it showed, so this segment carries only what follows."""
    text = extract_final_response(list(content))
    if shown_text and text.startswith(shown_text):
        text = text[len(shown_text) :].lstrip()
    steps = tuple(
        step_of(block, durations_ms.get(block.id))
        for block in content
        if isinstance(block, ToolUseBlock) and block.id not in hidden
    )
    return ReplyView(text=text, steps=steps)


def events_between(prev: ReplyView, curr: ReplyView) -> list[Event]:
    """New or changed steps, then the text: appended as a `delta`, else rewritten whole."""
    before = {step.id: step for step in prev.steps}
    events: list[Event] = [
        {"type": "step", "step": step.model_dump(exclude_none=True)}
        for step in curr.steps
        if before.get(step.id) != step
    ]
    if curr.text != prev.text:
        if curr.text.startswith(prev.text):
            events.append({"type": "delta", "text": curr.text[len(prev.text) :]})
        else:
            events.append({"type": "text", "text": curr.text})
    return events


def snapshot_events(view: ReplyView) -> list[Event]:
    """The whole view as events, for a reader who missed the ones that built it."""
    steps = [{"type": "step", "step": step.model_dump(exclude_none=True)} for step in view.steps]
    return [*steps, {"type": "text", "text": view.text}]
