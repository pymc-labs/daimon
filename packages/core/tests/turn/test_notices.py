"""The termination notice: one explanation per reason, platform-neutral."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from daimon.core.turn import TerminationReason, render_termination_notice
from daimon.core.turn.state import McpServerFailure, TextBlock, ToolUseBlock, TurnState

_ENDED_EARLY = [r for r in TerminationReason if r is not TerminationReason.COMPLETED]


def _tool(name: str, status: str) -> ToolUseBlock:
    return ToolUseBlock(
        kind="tool_use",
        id=f"tu_{name}",
        type="agent.tool_use",
        name=name,
        input={},
        status=status,  # type: ignore[arg-type]
    )


def test_a_completed_turn_has_no_notice() -> None:
    assert render_termination_notice(TerminationReason.COMPLETED) is None


@pytest.mark.parametrize("reason", _ENDED_EARLY, ids=str)
def test_every_early_end_explains_itself(reason: TerminationReason) -> None:
    notice = render_termination_notice(reason, request_id="rid_1")

    assert notice is not None
    assert notice.reason is reason
    assert notice.headline and notice.cause and notice.survived and notice.next_step
    assert len(notice.headline) <= 40, "the headline has to fit a status footer uncut"
    assert "rid_1" in notice.plain_text()


def test_reasons_a_turn_can_run_into_read_differently() -> None:
    """Every reason with its own copy says something no other reason says."""
    shared = {TerminationReason.REDUCER_BUG, TerminationReason.UNKNOWN}
    notices = [render_termination_notice(r) for r in _ENDED_EARLY if r not in shared]
    headlines = [n.headline for n in notices if n is not None]
    causes = [n.cause for n in notices if n is not None]
    assert len(set(headlines)) == len(headlines)
    assert len(set(causes)) == len(causes)


def test_the_notice_names_work_still_running_and_counts_what_finished() -> None:
    state = TurnState(
        content=[
            TextBlock(kind="text", text="working on it"),
            _tool("bash", "complete"),
            _tool("fetch", "failed"),
            _tool("fit_model", "pending"),
        ]
    )

    notice = render_termination_notice(TerminationReason.CONNECTION_LOST, state=state)

    assert notice is not None
    assert notice.in_flight == ("fit_model",)
    assert notice.finished_tools == 2
    assert notice.work_line(lambda n: f"`{n}`") == (
        "Still running when it ended: `fit_model`. Finished before that: 2 tool calls."
    )


def test_a_long_list_of_running_tools_is_shortened() -> None:
    state = TurnState(content=[_tool(f"t{i}", "pending") for i in range(8)])

    notice = render_termination_notice(TerminationReason.CEILING, state=state)

    assert notice is not None
    line = notice.work_line()
    assert line is not None and line.endswith("t4 and 3 more.")


def test_no_tool_work_means_no_work_line() -> None:
    notice = render_termination_notice(TerminationReason.UPSTREAM, state=TurnState())
    assert notice is not None and notice.work_line() is None


def test_an_empty_turn_after_mcp_failure_names_the_server() -> None:
    state = TurnState(
        mcp_failures=(
            McpServerFailure(
                server_name="notion",
                error_type="mcp_connection_failed_error",
                message="down",
                retry_status="exhausted",
            ),
        )
    )

    notice = render_termination_notice(TerminationReason.MCP_DEGRADED_EMPTY, state=state)

    assert notice is not None and "notion" in notice.cause


def test_a_rate_limit_says_when_to_retry() -> None:
    until = datetime(2026, 9, 28, 17, 45, tzinfo=UTC)
    notice = render_termination_notice(
        TerminationReason.RATE_LIMITED, state=TurnState(rate_limit_until=until)
    )
    assert notice is not None and "17:45 UTC" in notice.next_step


def test_the_ceiling_says_the_session_was_retired() -> None:
    notice = render_termination_notice(TerminationReason.CEILING)
    assert notice is not None
    assert "45-minute" in notice.cause
    assert "retired" in notice.survived


def test_plain_text_carries_every_field_in_order() -> None:
    notice = render_termination_notice(
        TerminationReason.UPSTREAM,
        state=TurnState(content=[_tool("bash", "pending")]),
        request_id="01ABC",
    )
    assert notice is not None
    lines = notice.plain_text().splitlines()
    assert lines[0] == f"{notice.headline}: {notice.cause}"
    assert lines[1].startswith("Still running when it ended: bash")
    assert lines[2:] == [notice.survived, notice.next_step, "Request id: 01ABC"]
