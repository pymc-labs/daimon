from __future__ import annotations

import dataclasses

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from daimon.core.errors import TurnError
from daimon.core.turn.state import (
    ContentBlock,
    Task,
    TextBlock,
    ToolUseBlock,
    TurnState,
    extract_final_response,
    extract_sealed_responses,
)


def test_turn_state_defaults_populate_empty_collections_when_constructed() -> None:
    state = TurnState()
    assert state.content == [], "content must default to empty list"
    assert state.tasks == [], "tasks must default to empty list"
    assert state.rate_limit_until is None
    assert state.stop_reason is None
    assert state.error is None
    assert state.seen_event_ids == frozenset(), (
        "seen_event_ids must default to empty frozenset for immutable state"
    )


def test_turn_state_is_frozen_so_reducers_must_return_new_instances() -> None:
    state = TurnState()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.stop_reason = BetaManagedAgentsSessionEndTurn(type="end_turn")  # type: ignore[misc]


def test_text_block_is_discriminated_by_kind_literal_when_instantiated() -> None:
    text: ContentBlock = TextBlock(kind="text", text="hello")
    assert text.kind == "text"
    # pyright narrows on `kind` — this cast is legal:
    if text.kind == "text":
        assert text.text == "hello"


def test_tool_use_block_defaults_status_to_pending_when_built() -> None:
    block = ToolUseBlock(
        kind="tool_use",
        id="tu_1",
        type="agent.tool_use",
        name="bash",
        input={"command": "ls"},
    )
    assert block.kind == "tool_use"
    assert block.status == "pending", "a freshly-observed tool_use event has no result yet"
    assert block.result_content is None
    assert block.is_error is False
    assert block.evaluated_permission is None
    assert block.mcp_server_name is None


def test_tool_use_block_round_trips_complete_status_and_result_when_set() -> None:
    block = ToolUseBlock(
        kind="tool_use",
        id="tu_1",
        type="agent.tool_use",
        name="bash",
        input={},
        status="complete",
        result_content=[BetaManagedAgentsTextBlock(type="text", text="ok")],
        is_error=False,
    )
    assert block.status == "complete"
    assert block.result_content is not None
    assert len(block.result_content) == 1


def test_turn_state_accepts_turn_error_on_error_field_when_upstream_fails() -> None:
    err = TurnError(kind="upstream", message="429")
    state = TurnState(error=err)
    assert state.error is err


def test_task_is_empty_placeholder_kept_for_forward_compat_when_referenced() -> None:
    task = Task()
    assert isinstance(task, Task), "Task is a placeholder; Managed Agents emits no task events yet"


class TestExtractFinalResponse:
    def test_multi_tool_returns_text_after_last_tool_use(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="I'll look that up. "),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text="Let me check another thing. "),
            ToolUseBlock(kind="tool_use", id="tu_2", type="agent.tool_use", name="read", input={}),
            TextBlock(kind="text", text="Here is the answer."),
        ]
        assert extract_final_response(content) == "Here is the answer."

    def test_no_tool_returns_all_text(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="Hello "),
            TextBlock(kind="text", text="world"),
        ]
        assert extract_final_response(content) == "Hello world"

    def test_empty_content_returns_empty_string(self) -> None:
        assert extract_final_response([]) == ""

    def test_zero_message_after_tools_returns_empty(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="I'll do that."),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
        ]
        assert extract_final_response(content) == ""

    def test_single_tool_with_final_text(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="Let me check."),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text="Done. The result is 42."),
        ]
        assert extract_final_response(content) == "Done. The result is 42."

    def test_only_tool_blocks_returns_empty(self) -> None:
        content: list[ContentBlock] = [
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            ToolUseBlock(kind="tool_use", id="tu_2", type="agent.tool_use", name="read", input={}),
        ]
        assert extract_final_response(content) == ""


class TestExtractSealedResponses:
    def test_long_text_followed_by_tool_is_returned_with_index(self) -> None:
        answer = "x" * 600
        content: list[ContentBlock] = [
            TextBlock(kind="text", text=answer),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text="Recap."),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [(0, answer)], (
            "a text block sealed by a tool use and >= min_chars should be returned"
        )

    def test_short_text_followed_by_tool_is_excluded(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="Let me check that."),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [], (
            "narration under min_chars stays suppressed"
        )

    def test_trailing_text_after_last_tool_is_excluded(self) -> None:
        content: list[ContentBlock] = [
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text="y" * 600),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [], (
            "unsealed trailing text is the final response, not a sealed block"
        )

    def test_multiple_sealed_blocks_returned_in_stream_order(self) -> None:
        first = "a" * 700
        second = "b" * 900
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="short narration"),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text=first),
            ToolUseBlock(kind="tool_use", id="tu_2", type="agent.tool_use", name="read", input={}),
            TextBlock(kind="text", text=second),
            ToolUseBlock(kind="tool_use", id="tu_3", type="agent.tool_use", name="edit", input={}),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [(2, first), (4, second)], (
            "every sealed block over the threshold is returned, oldest first"
        )

    def test_empty_content_returns_empty_list(self) -> None:
        assert extract_sealed_responses([], min_chars=500) == []
