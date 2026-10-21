"""Self-hosted neutral events use the existing host reducers without provider I/O."""

from datetime import UTC, datetime

import pytest
from daimon.core.turn.selfhosted_codec import display_batch, tool_display_id
from mux.contracts.events import Event, NativeProvenance, RequiredAction, RequiresActionPayload
from mux.errors import ScopeViolation


def event(type_, payload, provider="selfhosted"):
    return Event(
        id="event",
        session_id="session",
        sequence=1,
        type=type_,
        turn_id="turn",
        authority="record",
        observed_at=datetime.now(UTC),
        payload=payload,
        native=NativeProvenance(provider=provider, api_revision="daimon-harness/0"),
    )


def test_tool_confirmation_displays_exact_tool_input_and_recoverable_identity():
    value = event(
        "session.requires_action",
        RequiresActionPayload(
            actions=(
                RequiredAction(
                    id="approval",
                    kind="tool_confirmation",
                    call_id="call",
                    payload={"name": "bash", "input": {"command": "echo hi"}},
                ),
            )
        ).model_dump(mode="json"),
    )
    tool, pause = display_batch(value, session_id="session")
    assert tool.type == "agent.tool_use"
    assert tool.id == tool_display_id("call")
    assert tool.name == "bash"
    assert tool.input == {"command": "echo hi"}
    assert tool.evaluated_permission == "ask"
    assert pause.type == "session.status_idle"
    assert pause.stop_reason.type == "requires_action"
    assert pause.stop_reason.event_ids == [tool.id]


def test_usage_failure_remains_neutral_and_terminal_drives_error_path():
    value = event(
        "session.error",
        {"category": "upstream", "retry_status": "terminal", "native_code": "usage_unavailable"},
    )
    assert display_batch(value, session_id="session") == ()


def test_foreign_provider_is_refused():
    value = event("session.status_running", {"root_turn_id": "turn"}, provider="openai")
    with pytest.raises(ScopeViolation):
        display_batch(value, session_id="session")


@pytest.mark.parametrize("authority", ["record", "reconciled"])
def test_mcp_confirmation_preserves_server_and_denied_tool(authority):
    from daimon.core.tool_safety import ToolSafetyPolicy, decide_tool_call
    from daimon.core.turn.approvals import tool_calls_for
    from daimon.core.turn.reducers import apply
    from daimon.core.turn.state import TurnState

    value = event(
        "session.requires_action",
        RequiresActionPayload(
            actions=(
                RequiredAction(
                    id="approval",
                    kind="tool_confirmation",
                    call_id="call",
                    payload={"name": "reports/delete_report", "input": {"id": "report"}},
                ),
            )
        ).model_dump(mode="json"),
    ).model_copy(update={"authority": authority})
    tool, pause = display_batch(value, session_id="session")
    assert tool.type == "agent.mcp_tool_use"
    assert tool.mcp_server_name == "reports"
    assert tool.name == "delete_report"
    state = apply(TurnState(), tool)
    (call,) = tool_calls_for(state, [tool.id])
    verdict = decide_tool_call(
        ToolSafetyPolicy(enabled=True, denied=("reports/delete_report",)), call, attended=True
    )
    assert verdict.outcome == "deny"
    assert decide_tool_call(ToolSafetyPolicy(enabled=True), call, attended=True).outcome == "ask"


@pytest.mark.parametrize("name,executor", [("write", "agent"), ("reports/write", "mcp")])
def test_approval_execution_and_result_update_one_call_block(name, executor):
    from daimon.core.turn.reducers import apply
    from daimon.core.turn.state import ToolUseBlock, TurnState

    approval = event(
        "session.requires_action",
        RequiresActionPayload(
            actions=(
                RequiredAction(
                    id="approval-id",
                    kind="tool_confirmation",
                    call_id="call-id",
                    payload={"name": name, "input": {"path": "result.txt"}},
                ),
            )
        ).model_dump(mode="json"),
    )
    execution = event(
        "agent.tool_use",
        {
            "call_id": "call-id",
            "tool_name": name,
            "input": {"path": "result.txt"},
            "executor": executor,
        },
    )
    result = event(
        "agent.tool_result",
        {"call_id": "call-id", "content": [{"type": "text", "text": "written"}], "is_error": False},
    )
    state = TurnState()
    for value in (approval, execution, result):
        for display in display_batch(value, session_id="session"):
            state = apply(state, display)
    blocks = [block for block in state.content if isinstance(block, ToolUseBlock)]
    assert len(blocks) == 1
    assert blocks[0].id == tool_display_id("call-id")
    assert blocks[0].status == "complete"
    assert blocks[0].result_content and blocks[0].result_content[0].text == "written"
