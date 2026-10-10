"""OpenAI display compatibility proofs; no registration or provider calls."""

from datetime import UTC, datetime

import pytest
from daimon.core.turn.openai_codec import display_batch, display_event
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from mux.contracts.events import Event, NativeProvenance
from mux.errors import ScopeViolation, UnsupportedCapability
from pydantic import JsonValue


def event(kind: str, payload: dict[str, JsonValue], **fields: object) -> Event:
    return Event.model_validate(
        {
            "id": "live-event",
            "session_id": "session",
            "type": kind,
            "payload": payload,
            "sequence": 0,
            "authority": "record",
            "observed_at": datetime(2026, 10, 10, tzinfo=UTC),
            "native": NativeProvenance(provider="openai", api_revision="2026-10-09"),
            **fields,
        }
    )


def test_final_message_uses_stable_item_identity_across_recovery() -> None:
    live = event(
        "agent.message",
        {"item_id": "item", "content": [{"type": "text", "text": "answer"}]},
    )
    replay = live.model_copy(update={"id": "saved-event", "authority": "reconciled"})
    first = display_event(live, session_id="session")
    second = display_event(replay, session_id="session")
    assert first is not None and second is not None
    assert first.id == second.id == "openai:item:item"
    state = apply(apply(TurnState(), first), second)
    assert len(state.content) == 1
    assert isinstance(state.content[0], TextBlock) and state.content[0].text == "answer"
    assert state.usage_totals == TurnState().usage_totals


@pytest.mark.parametrize("executor,server", [("agent", None), ("mcp", "daimon-mcp")])
@pytest.mark.parametrize("is_error", [False, True])
def test_native_tool_invocation_and_result_fold_with_real_call_identity(
    executor: str, server: str | None, is_error: bool
) -> None:
    use = event(
        "agent.tool_use",
        {
            "call_id": "call",
            "tool_name": "bash",
            "input": {"command": "pwd"},
            "executor": executor,
            "mcp_server": server,
        },
    )
    result = event(
        "agent.tool_result",
        {
            "call_id": "call",
            "content": [{"type": "text", "text": "/workspace"}],
            "is_error": is_error,
        },
    )
    first = display_event(use, session_id="session")
    second = display_event(result, session_id="session")
    assert first is not None and second is not None
    state = apply(apply(TurnState(), first), second)
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.id == "openai:tool:call"
    assert block.status == ("failed" if is_error else "complete")
    assert block.is_error == is_error
    assert state.finished_tool_ids == ("openai:tool:call",)


@pytest.mark.parametrize("outcome", ["completed", "interrupted", "errored", "terminated"])
def test_root_outcome_keeps_the_neutral_truth_without_billing(outcome: str) -> None:
    original = event(
        "session.turn_ended", {"root_turn_id": "root", "outcome": outcome}, turn_id="root"
    )
    native = display_event(original, session_id="session")
    assert native is not None and native.type == "session.status_idle"
    assert native.id == "openai:turn:root:ended"
    state = apply(TurnState(), native)
    assert state.stop_reason is not None
    assert state.stop_reason.type == ("retries_exhausted" if outcome == "errored" else "end_turn")
    assert original.payload["outcome"] == outcome
    assert state.usage_totals == TurnState().usage_totals


@pytest.mark.parametrize(
    "authority,thread", [("preview", None), ("gap", None), ("record", "child")]
)
def test_preview_gap_and_child_work_cannot_reach_root_reducers(
    authority: str, thread: str | None
) -> None:
    original = event(
        "session.turn_ended",
        {"root_turn_id": "root", "outcome": "completed"},
        turn_id="root",
        authority=authority,
        thread_id=thread,
    )
    assert display_event(original, session_id="session") is None


@pytest.mark.parametrize("kind", ["function_result", "environment_connection", "native"])
def test_required_results_and_native_actions_are_never_autoapproved(kind: str) -> None:
    original = event("session.requires_action", {"actions": [{"id": "call", "kind": kind}]})
    with pytest.raises(UnsupportedCapability, match="host_required_action_executor"):
        display_event(original, session_id="session")


def test_genuine_confirmation_routes_the_display_invocation_id() -> None:
    original = event(
        "session.requires_action",
        {
            "actions": [
                {
                    "id": "call",
                    "kind": "tool_confirmation",
                    "native_type": "computer_use_approval_request",
                    "payload": {
                        "type": "computer_use_approval_request",
                        "request_id": "call",
                        "turn_id": "root",
                        "request": {
                            "type": "browser_origin_access",
                            "origin": "https://example.com",
                        },
                    },
                }
            ]
        },
        turn_id="root",
    )
    native = display_event(original, session_id="session")
    assert native is not None and native.type == "session.status_idle"
    assert native.stop_reason.type == "requires_action"
    assert native.stop_reason.event_ids == ["openai:tool:call"]


def test_foreign_session_fails_before_decoding_even_a_preview() -> None:
    original = event("native.unknown", {}, authority="preview", session_id="other")
    with pytest.raises(ScopeViolation):
        display_event(original, session_id="session")


def test_openai_usage_is_not_an_anthropic_model_span() -> None:
    original = event("usage.observed", {"observation_id": "openai:turn:root", "revision": 1})
    assert display_event(original, session_id="session") is None


def test_origin_request_is_visible_to_existing_host_policy_before_pause():
    from daimon.core.turn.approvals import tool_calls_for

    original = event(
        "session.requires_action",
        {
            "actions": [
                {
                    "id": "call",
                    "kind": "tool_confirmation",
                    "native_type": "computer_use_approval_request",
                    "payload": {
                        "type": "computer_use_approval_request",
                        "request_id": "call",
                        "turn_id": "root",
                        "request": {
                            "type": "browser_origin_access",
                            "origin": "https://example.com",
                            "reason": "Read the requested page",
                        },
                    },
                }
            ]
        },
        turn_id="root",
    )
    records = display_batch(original, session_id="session")
    assert len(records) == 2
    state = TurnState()
    for record in records:
        state = apply(state, record)
    calls = tool_calls_for(state, ["openai:tool:call"])
    assert len(calls) == 1
    assert calls[0].tool_name == "browser_origin_access"
    assert calls[0].input == {"origin": "https://example.com", "reason": "Read the requested page"}
    assert state.stop_reason is not None and state.stop_reason.type == "requires_action"
    assert original.type == "session.requires_action"


def test_forged_generic_confirmation_cannot_display_as_an_origin_prompt():
    original = event(
        "session.requires_action",
        {"actions": [{"id": "call", "kind": "tool_confirmation"}]},
        turn_id="root",
    )
    with pytest.raises(UnsupportedCapability):
        display_batch(original, session_id="session")


def test_replayed_user_image_preserves_base64_input_anchor() -> None:
    anchor = event(
        "user.message",
        {
            "input_id": "input",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image", "media_type": "image/png", "data_base64": "aW1hZ2U="},
            ],
        },
    )
    display = display_event(anchor, session_id="session")
    assert display.type == "user.message"
    assert display.content[1].source.type == "base64"
    assert display.content[1].source.data == "aW1hZ2U="
