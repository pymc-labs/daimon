"""The web reply view of a turn state, and the events between two views."""

from __future__ import annotations

from daimon.adapters.web.stream_events import ReplyView, events_between, view_of
from daimon.core.stores.domain import WebStep
from daimon.core.turn.state import TextBlock, ToolUseBlock


def _tool(status: str = "pending") -> ToolUseBlock:
    return ToolUseBlock(
        kind="tool_use",
        id="t1",
        type="agent.mcp_tool_use",
        name="search_issues",
        input={},
        mcp_server_name="linear",
        status=status,  # pyright: ignore[reportArgumentType]
    )


def test_text_after_the_last_tool_is_the_answer() -> None:
    view = view_of(
        [
            TextBlock(kind="text", text="Looking."),
            _tool("complete"),
            TextBlock(kind="text", text="Found it."),
        ],
        durations_ms={"t1": 900},
    )
    assert view.text == "Found it."
    assert view.steps == (
        WebStep(id="t1", label="Search issues", detail="linear", status="done", ms=900),
    )


def test_a_running_step_has_no_duration() -> None:
    [step] = view_of([_tool()], durations_ms={"t1": 5}).steps
    assert (step.status, step.ms) == ("running", None)


def test_growing_text_is_a_delta_and_a_rewrite_is_text() -> None:
    assert events_between(ReplyView(text="Hel"), ReplyView(text="Hello")) == [
        {"type": "delta", "text": "lo"}
    ]
    assert events_between(ReplyView(text="Looking."), ReplyView(text="Found")) == [
        {"type": "text", "text": "Found"}
    ]
    assert events_between(ReplyView(text="same"), ReplyView(text="same")) == []


def test_only_new_or_changed_steps_are_sent() -> None:
    running = WebStep(id="a", label="A", status="running")
    done = running.model_copy(update={"status": "done", "ms": 10})
    events = events_between(ReplyView(steps=(running,)), ReplyView(steps=(done,)))
    assert events == [
        {"type": "step", "step": {"id": "a", "label": "A", "status": "done", "ms": 10}}
    ]
    assert events_between(ReplyView(steps=(done,)), ReplyView(steps=(done,))) == []


def test_a_later_segment_shows_only_what_follows() -> None:
    content = [_tool("complete"), TextBlock(kind="text", text="Before. After.")]
    view = view_of(content, durations_ms={}, hidden={"t1"}, shown_text="Before.")
    assert (view.text, view.steps) == ("After.", ())
