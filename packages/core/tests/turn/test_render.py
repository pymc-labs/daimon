from __future__ import annotations

from daimon.core.errors import TurnError
from daimon.core.turn.reducers import apply
from daimon.core.turn.render import (
    BlockAdded,
    RenderDelta,
    TextAppend,
    diff,
)
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState

from .conftest import (
    make_agent_message,
    make_end_turn,
    make_status_idle,
    make_tool_result,
    make_tool_use,
)


def test_diff_is_empty_when_states_are_equal() -> None:
    state = TurnState()
    delta = diff(state, state)
    assert delta == RenderDelta()
    assert delta.is_empty() is True


def test_diff_against_none_prev_treats_every_block_as_added() -> None:
    state = TurnState(content=[TextBlock(kind="text", text="hi")])
    delta = diff(None, state)
    assert delta.block_additions == [
        BlockAdded(block_index=0, block=TextBlock(kind="text", text="hi"))
    ]
    assert delta.is_empty() is False


def test_diff_reports_text_append_when_last_text_block_grew() -> None:
    prev = TurnState()
    prev = apply(prev, make_agent_message(event_id="sevt_1", text="hello"))

    curr = apply(prev, make_agent_message(event_id="sevt_2", text=" world"))

    delta = diff(prev, curr)
    assert delta.text_appends == [TextAppend(block_index=0, appended_text=" world")]
    assert delta.block_additions == []
    assert delta.block_status_changes == []


def test_diff_reports_block_added_when_new_tool_use_appears() -> None:
    prev = TurnState()
    prev = apply(prev, make_agent_message(event_id="sevt_1", text="before"))

    curr = apply(
        prev,
        make_tool_use(event_id="tu_1", name="bash", input={"c": "ls"}),
    )

    delta = diff(prev, curr)
    assert delta.text_appends == []
    assert len(delta.block_additions) == 1
    added = delta.block_additions[0]
    assert added.block_index == 1
    assert isinstance(added.block, ToolUseBlock)
    assert added.block.name == "bash"
    assert added.block.status == "pending"


def test_diff_reports_status_change_when_tool_result_completes_block() -> None:
    prev = TurnState()
    prev = apply(prev, make_tool_use(event_id="tu_1", name="bash"))

    curr = apply(
        prev,
        make_tool_result(event_id="r_1", tool_use_id="tu_1", text="ok"),
    )

    delta = diff(prev, curr)
    assert delta.block_additions == []
    assert delta.text_appends == []
    assert len(delta.block_status_changes) == 1
    change = delta.block_status_changes[0]
    assert change.block_index == 0
    assert change.block.status == "complete"
    assert change.block.result_content is not None


def test_diff_reports_stop_reason_when_set_for_first_time() -> None:
    prev = TurnState()
    curr = apply(prev, make_status_idle(event_id="sevt_1", stop_reason=make_end_turn()))
    delta = diff(prev, curr)
    assert delta.stop_reason_set is not None
    assert delta.stop_reason_set.type == "end_turn"


def test_diff_does_not_re_emit_stop_reason_when_unchanged() -> None:
    state = apply(TurnState(), make_status_idle(event_id="sevt_1"))
    delta = diff(state, state)
    assert delta.stop_reason_set is None


def test_diff_reports_error_when_newly_set_on_curr() -> None:
    prev = TurnState()
    err = TurnError(kind="upstream", message="429")
    curr = TurnState(error=err)
    delta = diff(prev, curr)
    assert delta.error_set is err


def test_diff_combines_text_append_and_block_added_when_both_change() -> None:
    prev = TurnState()
    prev = apply(prev, make_agent_message(event_id="sevt_1", text="hi"))

    curr = apply(prev, make_agent_message(event_id="sevt_2", text="!"))
    curr = apply(curr, make_tool_use(event_id="tu_1", name="bash"))

    delta = diff(prev, curr)
    assert delta.text_appends == [TextAppend(block_index=0, appended_text="!")]
    assert len(delta.block_additions) == 1
    assert delta.block_additions[0].block_index == 1


def test_is_empty_returns_false_when_any_field_populated() -> None:
    assert RenderDelta().is_empty() is True
    assert RenderDelta(stop_reason_set=make_end_turn()).is_empty() is False
    assert (
        RenderDelta(text_appends=[TextAppend(block_index=0, appended_text="x")]).is_empty() is False
    )
    assert (
        RenderDelta(
            block_additions=[BlockAdded(block_index=0, block=TextBlock(kind="text", text="x"))]
        ).is_empty()
        is False
    )
