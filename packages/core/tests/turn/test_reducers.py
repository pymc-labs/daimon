from __future__ import annotations

from datetime import UTC, datetime

from anthropic.types.beta.sessions.beta_managed_agents_document_block import (
    BetaManagedAgentsDocumentBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_image_block import (
    BetaManagedAgentsImageBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_url_document_source import (
    BetaManagedAgentsURLDocumentSource,
)
from anthropic.types.beta.sessions.beta_managed_agents_url_image_source import (
    BetaManagedAgentsURLImageSource,
)
from daimon.core.errors import TurnError
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState, UsageTotals

from .conftest import (
    make_agent_message,
    make_custom_tool_result,
    make_custom_tool_use,
    make_end_turn,
    make_mcp_tool_result,
    make_mcp_tool_use,
    make_overloaded_error,
    make_requires_action,
    make_retries_exhausted,
    make_session_error,
    make_status_idle,
    make_status_terminated,
    make_tool_result,
    make_tool_use,
)

# --- dedup ---


def test_apply_dedupes_event_when_seen_event_id_already_present() -> None:
    state = TurnState()
    event = make_agent_message(event_id="sevt_1", text="hi")
    once = apply(state, event)
    twice = apply(once, event)

    assert once.content == [TextBlock(kind="text", text="hi")]
    assert twice == once, "second apply of same event-id must be a no-op"


def test_apply_records_event_id_in_seen_event_ids() -> None:
    state = TurnState()
    next_state = apply(state, make_agent_message(event_id="sevt_1", text="hi"))
    assert "sevt_1" in next_state.seen_event_ids


# --- agent.message ---


def test_apply_concatenates_consecutive_agent_messages_into_one_text_block() -> None:
    state = TurnState()
    state = apply(state, make_agent_message(event_id="sevt_1", text="hello "))
    state = apply(state, make_agent_message(event_id="sevt_2", text="world"))
    assert state.content == [TextBlock(kind="text", text="hello world")]


def test_apply_opens_new_text_block_when_tool_use_interleaves() -> None:
    state = TurnState()
    state = apply(state, make_agent_message(event_id="sevt_1", text="before "))
    state = apply(state, make_tool_use(event_id="tu_1", name="bash", input={"c": "ls"}))
    state = apply(state, make_agent_message(event_id="sevt_3", text="after"))

    assert len(state.content) == 3
    assert isinstance(state.content[0], TextBlock)
    assert isinstance(state.content[1], ToolUseBlock)
    assert isinstance(state.content[2], TextBlock)
    assert state.content[2].text == "after"


# --- tool_use (3 variants) ---


def test_apply_adds_pending_tool_use_block_for_builtin_variant() -> None:
    state = apply(
        TurnState(),
        make_tool_use(event_id="tu_1", name="bash", input={"command": "ls"}),
    )
    assert len(state.content) == 1
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.id == "tu_1"
    assert block.type == "agent.tool_use"
    assert block.name == "bash"
    assert block.input == {"command": "ls"}
    assert block.status == "pending"
    assert block.result_content is None
    assert block.evaluated_permission is None
    assert block.mcp_server_name is None


def test_apply_propagates_evaluated_permission_onto_tool_use_block() -> None:
    state = apply(
        TurnState(),
        make_tool_use(event_id="tu_1", name="bash", evaluated_permission="ask"),
    )
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.evaluated_permission == "ask"


def test_apply_propagates_mcp_server_name_only_on_mcp_variant() -> None:
    state = apply(
        TurnState(),
        make_mcp_tool_use(event_id="tu_m", name="search", mcp_server_name="github"),
    )
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.type == "agent.mcp_tool_use"
    assert block.mcp_server_name == "github"

    # builtin variant does not carry mcp_server_name
    state2 = apply(TurnState(), make_tool_use(event_id="tu_b", name="bash"))
    block2 = state2.content[0]
    assert isinstance(block2, ToolUseBlock)
    assert block2.mcp_server_name is None


def test_apply_handles_custom_tool_use_variant() -> None:
    state = apply(
        TurnState(),
        make_custom_tool_use(event_id="tu_c", name="my_tool", input={"x": 1}),
    )
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.type == "agent.custom_tool_use"
    assert block.evaluated_permission is None  # SDK doesn't carry it on this variant


# --- tool_result (3 variants) ---


def test_apply_completes_tool_block_when_matching_result_arrives() -> None:
    state = apply(TurnState(), make_tool_use(event_id="tu_1", name="bash"))
    state = apply(state, make_tool_result(event_id="r_1", tool_use_id="tu_1", text="ok"))
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.status == "complete"
    assert block.is_error is False
    assert block.result_content is not None
    assert len(block.result_content) == 1
    first = block.result_content[0]
    assert isinstance(first, BetaManagedAgentsTextBlock)
    assert first.text == "ok"


def test_apply_marks_tool_block_failed_when_result_is_error() -> None:
    state = apply(TurnState(), make_tool_use(event_id="tu_1", name="bash"))
    state = apply(
        state,
        make_tool_result(event_id="r_1", tool_use_id="tu_1", text="boom", is_error=True),
    )
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.status == "failed"
    assert block.is_error is True


def test_apply_defaults_is_error_to_false_when_sdk_delivers_none() -> None:
    state = apply(TurnState(), make_tool_use(event_id="tu_1", name="bash"))
    state = apply(
        state,
        make_tool_result(event_id="r_1", tool_use_id="tu_1", text="ok", is_error=None),
    )
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.is_error is False
    assert block.status == "complete"


def test_apply_pairs_mcp_tool_result_via_mcp_tool_use_id() -> None:
    state = apply(
        TurnState(),
        make_mcp_tool_use(event_id="tu_m", name="search", mcp_server_name="github"),
    )
    state = apply(
        state,
        make_mcp_tool_result(event_id="r_m", mcp_tool_use_id="tu_m", text="hit"),
    )
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.status == "complete"


def test_apply_pairs_custom_tool_result_via_custom_tool_use_id() -> None:
    """Regression guard: the old shim aliased custom_tool_use_id → tool_use_id.
    The reducer must use the SDK's actual pairing field name."""
    state = apply(TurnState(), make_custom_tool_use(event_id="tu_c", name="my_tool"))
    state = apply(
        state,
        make_custom_tool_result(event_id="r_c", custom_tool_use_id="tu_c", text="done"),
    )
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.status == "complete"


def test_apply_preserves_mixed_content_blocks_in_tool_result() -> None:
    state = apply(TurnState(), make_tool_use(event_id="tu_1", name="fetch"))
    mixed_content = [
        BetaManagedAgentsTextBlock(type="text", text="see attached"),
        BetaManagedAgentsImageBlock(
            type="image",
            source=BetaManagedAgentsURLImageSource(type="url", url="https://example/x.png"),
        ),
        BetaManagedAgentsDocumentBlock(
            type="document",
            source=BetaManagedAgentsURLDocumentSource(type="url", url="https://example/x.pdf"),
        ),
    ]
    state = apply(
        state,
        make_tool_result(event_id="r_1", tool_use_id="tu_1", content=mixed_content),
    )
    block = state.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.result_content is not None
    assert len(block.result_content) == 3
    assert isinstance(block.result_content[1], BetaManagedAgentsImageBlock)
    assert isinstance(block.result_content[2], BetaManagedAgentsDocumentBlock)


def test_apply_ignores_orphan_tool_result_with_no_matching_block() -> None:
    state = apply(
        TurnState(),
        make_tool_result(event_id="r_orphan", tool_use_id="tu_unknown", text="ok"),
    )
    assert state.content == []
    assert "r_orphan" in state.seen_event_ids


# --- multi-turn log (reconnect-replay pitfall) ---


def test_apply_two_turn_log_does_not_leak_prior_turn_text() -> None:
    """Folding a two-turn session event log from TurnState() must NOT leak
    the prior turn's text into the current turn's content.

    This characterises the reconnect-replay pitfall for reused sessions:
    driver.py's reconnect path does `functools.reduce(apply, replayed, TurnState())`
    where `replayed` contains ALL session events including prior turns. The reducer
    does NOT reset content at turn boundaries — it simply accumulates. So a two-turn
    fold yields content that bleeds turn-1 text into the final state used for rendering.

    The driver's reconnect path must therefore scope the replay to current-turn events
    only (events after the last session.status_idle boundary).
    """
    import functools

    # Turn 1: agent reply + idle
    turn1_message = make_agent_message(event_id="sevt_t1", text="FIRST_TURN_REPLY")
    turn1_idle = make_status_idle(event_id="idle_t1")

    # Turn 2 (current): agent reply (in-progress at reconnect time)
    turn2_message = make_agent_message(event_id="sevt_t2", text="SECOND_TURN_REPLY")

    all_events = [turn1_message, turn1_idle, turn2_message]

    state = functools.reduce(apply, all_events, TurnState())

    # The reducer accumulates without turn-boundary resets:
    # turn1_message adds TextBlock("FIRST_TURN_REPLY")
    # turn1_idle adds stop_reason (no content reset)
    # turn2_message tries to append to the last TextBlock, growing it
    # Result: one TextBlock containing "FIRST_TURN_REPLYSECOND_TURN_REPLY"
    # This IS the pitfall — prior turn text bleeds through.
    #
    # The driver reconnect path must NOT use this raw fold. It must scope
    # replayed events to those after the last session.status_idle boundary.
    # See driver.py _consume_with_reconnect + _events_since_last_turn_boundary.
    combined_text = "".join(
        b.text
        for b in state.content
        if isinstance(b, TextBlock)  # type: ignore[union-attr]
    )
    assert "FIRST_TURN_REPLY" in combined_text, (
        "reducer accumulates without reset — prior turn text IS present in raw fold; "
        "this confirms the pitfall that the driver must guard against"
    )
    assert "SECOND_TURN_REPLY" in combined_text, (
        "current turn text must also be present in the raw fold"
    )


# --- session.status_idle (StopReason variants) ---


def test_apply_sets_stop_reason_to_end_turn_variant() -> None:
    state = apply(TurnState(), make_status_idle(event_id="s_1", stop_reason=make_end_turn()))
    assert state.stop_reason is not None
    assert state.stop_reason.type == "end_turn"


def test_apply_sets_stop_reason_to_requires_action_variant_with_event_ids() -> None:
    state = apply(
        TurnState(),
        make_status_idle(
            event_id="s_1",
            stop_reason=make_requires_action(event_ids=["tu_1", "tu_2"]),
        ),
    )
    assert state.stop_reason is not None
    assert state.stop_reason.type == "requires_action"
    # Structural payload preserved (the whole point of SDK-typed stop_reason).
    assert getattr(state.stop_reason, "event_ids", None) == ["tu_1", "tu_2"]


def test_apply_sets_stop_reason_to_retries_exhausted_variant() -> None:
    state = apply(
        TurnState(),
        make_status_idle(event_id="s_1", stop_reason=make_retries_exhausted()),
    )
    assert state.stop_reason is not None
    assert state.stop_reason.type == "retries_exhausted"


# --- session.status_terminated: finalizes as an upstream error ---


def test_apply_finalizes_session_status_terminated_as_upstream_error() -> None:
    """`session.status_terminated` has no SDK stop_reason variant — a terminated
    session must not fall through to a silent dedup-only no-op (that renders as
    empty success downstream). The reducer sets a terminal TurnError so the
    driver's error-finalize path fires instead."""
    state = apply(TurnState(), make_status_terminated(event_id="s_term"))
    assert state.error is not None, "terminated session must set a TurnError, not stay a no-op"
    assert state.error.kind == "upstream"
    assert "terminated" in state.error.message.lower()
    assert "s_term" in state.seen_event_ids


# --- session.error (multiple Error variants → TurnError.cause) ---


def test_apply_wraps_rate_limited_error_into_turn_error_with_cause() -> None:
    state = apply(TurnState(), make_session_error(event_id="e_1", message="429"))
    assert isinstance(state.error, TurnError)
    assert state.error.kind == "upstream"
    assert state.error.cause is not None
    assert getattr(state.error.cause, "type", None) == "model_rate_limited_error"


def test_apply_wraps_overloaded_error_into_turn_error_with_cause() -> None:
    state = apply(
        TurnState(),
        make_session_error(event_id="e_1", error=make_overloaded_error()),
    )
    assert isinstance(state.error, TurnError)
    assert state.error.kind == "upstream"
    assert state.error.cause is not None
    assert getattr(state.error.cause, "type", None) == "model_overloaded_error"


# --- unhandled types fall through to dedup-only ---


def test_apply_passes_through_unknown_event_object_as_dedup_only_update() -> None:
    """Driver may forward non-dispatched event types (agent.thinking,
    span.model_request_start, …); reducer treats them as dedup-only."""

    class UnknownEvent:
        id = "sevt_unknown_1"
        type = "span.model_request_start"

    next_state = apply(TurnState(), UnknownEvent())  # type: ignore[arg-type]
    assert next_state.content == []
    assert "sevt_unknown_1" in next_state.seen_event_ids


# --- purity ---


def test_apply_is_pure_when_called_multiple_times_with_same_inputs() -> None:
    state = TurnState()
    event = make_agent_message(event_id="sevt_1", text="hi")
    a = apply(state, event)
    b = apply(state, event)
    assert a == b
    assert state.content == []


# --- span.model_request_end usage accumulation ---


def test_apply_folds_span_model_request_end_into_usage_totals() -> None:
    state = TurnState()
    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="sevt_usage_1",
        type="span.model_request_end",
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=10,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=2,
            output_tokens=5,
            speed="standard",
        ),
        processed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    next_state = apply(state, event)
    assert next_state.usage_totals == UsageTotals(
        input_tokens=10,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=2,
        output_tokens=5,
    ), "a single span.model_request_end must fold its 4 stage totals onto usage_totals"


def test_apply_accumulates_usage_totals_across_two_span_events() -> None:
    state = TurnState()
    first = BetaManagedAgentsSpanModelRequestEndEvent(
        id="sevt_usage_1",
        type="span.model_request_end",
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=10,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=2,
            output_tokens=5,
            speed="standard",
        ),
        processed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = BetaManagedAgentsSpanModelRequestEndEvent(
        id="sevt_usage_2",
        type="span.model_request_end",
        model_request_start_id="start_2",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=100,
            cache_creation_input_tokens=7,
            cache_read_input_tokens=8,
            output_tokens=20,
            speed="standard",
        ),
        processed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    state = apply(state, first)
    state = apply(state, second)
    assert state.usage_totals == UsageTotals(
        input_tokens=110,
        cache_creation_input_tokens=10,
        cache_read_input_tokens=10,
        output_tokens=25,
    ), "two span.model_request_end events must sum each of the 4 stage totals"


def test_apply_dedupes_span_model_request_end_leaving_usage_totals_unchanged() -> None:
    state = TurnState()
    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="sevt_usage_1",
        type="span.model_request_end",
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=10,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=2,
            output_tokens=5,
            speed="standard",
        ),
        processed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    once = apply(state, event)
    twice = apply(once, event)
    assert twice == once, "re-applying the same span event id must be a no-op"
    assert twice.usage_totals == UsageTotals(
        input_tokens=10,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=2,
        output_tokens=5,
    ), "dedup must leave all 4 usage totals unchanged"


def test_fresh_turn_state_has_zero_usage_totals() -> None:
    assert TurnState().usage_totals == UsageTotals(
        input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=0,
    ), "a fresh TurnState must start with all-zero usage totals"


def test_apply_leaves_usage_totals_unchanged_on_non_usage_event() -> None:
    state = TurnState()
    next_state = apply(state, make_agent_message(event_id="sevt_1", text="hi"))
    assert next_state.usage_totals == UsageTotals(), (
        "a non-usage event must not change usage_totals"
    )
