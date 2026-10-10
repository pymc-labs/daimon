"""Approved stop and refusal words, including spacing and short references."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest
from daimon.core.turn import TerminationReason, render_termination_notice
from daimon.core.turn.errors import AdmissionDenialReason
from daimon.core.turn.notices import (
    RefusalNouns,
    admission_refusal_text,
    fit_notice,
    short_ref,
)
from daimon.core.turn.state import McpServerFailure, ToolUseBlock, TurnState


def _tool(name: str, status: str) -> ToolUseBlock:
    return ToolUseBlock(
        kind="tool_use",
        id=f"tu_{name}",
        type="agent.tool_use",
        name=name,
        input={},
        status=status,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("reason", "title", "action"),
    [
        (
            TerminationReason.INTERRUPTED,
            "Stopped. Changes already made stay made.",
            "@mention Daimon to carry on.",
        ),
        (
            TerminationReason.INTERRUPT_TIMEOUT,
            "Stopping, but not confirmed. It may still be finishing.",
            "Wait a minute before you @mention Daimon again.",
        ),
        (
            TerminationReason.CONNECTION_LOST,
            "Lost the connection. Daimon may still be working.",
            "@mention Daimon to ask what it finished.",
        ),
        (
            TerminationReason.UPSTREAM,
            "Daimon's AI service failed on this reply.",
            "@mention Daimon to try again.",
        ),
        (TerminationReason.RATE_LIMITED, "Daimon's AI service is busy.", "Try again in a minute."),
        (
            TerminationReason.SESSION_TERMINATED,
            "Daimon stopped mid-reply. Its working files may be gone.",
            "@mention Daimon to carry on.",
        ),
        (
            TerminationReason.MCP_DEGRADED_EMPTY,
            "The connection to a tool failed, so there's no reply.",
            "Ask to reconnect it, or ask again without it.",
        ),
        (
            TerminationReason.RETRYING_UNSETTLED,
            "The AI kept failing, so there's no reply.",
            "Wait a moment, then @mention Daimon again.",
        ),
        (
            TerminationReason.REQUIRES_ACTION,
            "Daimon couldn't get the approval it needed.",
            "@mention Daimon to try again.",
        ),
        (
            TerminationReason.CEILING,
            "This took too long, so Daimon stopped waiting.",
            "@mention Daimon to try again.",
        ),
        (
            TerminationReason.RECOVERY_CANCELLED,
            "Stopped. Daimon lost its working files.",
            "@mention Daimon to carry on.",
        ),
        (
            TerminationReason.RECOVERY_FAILED,
            "Daimon lost its working files and couldn't continue.",
            "@mention Daimon to try again.",
        ),
        (
            TerminationReason.ADMISSION_CONCURRENCY_SHED,
            "Daimon is too busy right now.",
            "@mention Daimon again in a moment.",
        ),
        (
            TerminationReason.ADMISSION_BALANCE_DEPLETED,
            "Your team's Daimon credit has run out.",
            "Ask an admin to top up.",
        ),
        (
            TerminationReason.ADMISSION_CAP_EXCEEDED,
            "You've used your monthly limit.",
            "Ask the team running Daimon to raise it.",
        ),
        (
            TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED,
            "This channel's budget is used up.",
            "An admin can raise it.",
        ),
        (
            TerminationReason.ADMISSION_CHANNEL_PROTECTED,
            "Daimon can't reply in this channel.",
            "Ask somewhere else, or ask an admin.",
        ),
        (
            TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE,
            "team-020-analyst only works in other channels.",
            "Ask it there.",
        ),
        (
            TerminationReason.ADMISSION_CHANNEL_ISOLATED,
            "This channel only uses its own agents.",
            "Ask an admin to pick one.",
        ),
        (
            TerminationReason.ADMISSION_DENIED,
            "Daimon couldn't accept this request.",
            "Ask an admin.",
        ),
        (
            TerminationReason.MISSING_CONFIG,
            "Daimon isn't set up in this channel yet.",
            "Ask an admin to run `/agent-setup`.",
        ),
        (
            TerminationReason.RESOLVER_MISS,
            "This channel's setup is out of date.",
            "Ask an admin to check `/agent-setup`.",
        ),
        (
            TerminationReason.SESSION_PREPARATION_FAILED,
            "A settings change hasn't applied yet.",
            "@mention Daimon again in a little while.",
        ),
        (
            TerminationReason.SESSION_BUSY,
            "Daimon is still working in this conversation.",
            "Wait for it to finish, then @mention Daimon.",
        ),
        (
            TerminationReason.SESSION_AGENT_MISMATCH,
            "team-020-analyst can't continue this conversation.",
            "Start a new conversation to talk to it.",
        ),
        (TerminationReason.UNKNOWN, "Something went wrong.", "@mention Daimon to try again."),
        (TerminationReason.REDUCER_BUG, "Something went wrong.", "@mention Daimon to try again."),
    ],
)
def test_approved_termination_rows(reason: TerminationReason, title: str, action: str) -> None:
    notice = render_termination_notice(
        reason, agent_name="team-020-analyst", request_id="01a7c3qx2"
    )
    assert notice is not None
    assert (notice.title, notice.next_step) == (title, action)
    assert notice.plain_text() == f"{title}\n\n{action}\n\nRef 7C3QX2"
    assert "The conversation and its workspace are kept." not in notice.plain_text()


def test_completed_has_no_notice() -> None:
    assert render_termination_notice(TerminationReason.COMPLETED) is None


@pytest.mark.parametrize(
    ("reason", "action"),
    [
        (TerminationReason.INTERRUPTED, "Send a message to carry on."),
        (TerminationReason.UPSTREAM, "Send your message again."),
        (TerminationReason.CONNECTION_LOST, "Send a message to ask what it finished."),
        (TerminationReason.SESSION_BUSY, "Wait for it to finish, then send a message."),
    ],
)
def test_dm_actions(reason: TerminationReason, action: str) -> None:
    notice = render_termination_notice(reason, in_dm=True)
    assert notice is not None and notice.next_step == action


def test_work_and_ref_are_small_separate_lines() -> None:
    state = TurnState(
        content=[
            _tool("bash", "complete"),
            _tool("fetch", "failed"),
            _tool("fit_model", "pending"),
        ]
    )
    notice = render_termination_notice(
        TerminationReason.CONNECTION_LOST, state=state, request_id="a7c3qx2"
    )
    assert notice is not None
    assert notice.work_line(lambda name: f"`{name}`") == (
        "Still running when it ended: `fit_model`. Finished before that: 2 tool calls."
    )
    assert notice.plain_text().endswith(
        "Still running when it ended: fit_model. Finished before that: 2 tool calls.\n\nRef 7C3QX2"
    )


def test_server_name_and_rate_horizon() -> None:
    state = TurnState(
        mcp_failures=(
            McpServerFailure(
                server_name="Notion",
                error_type="mcp_connection_failed_error",
                message="down",
                retry_status="exhausted",
            ),
        )
    )
    notice = render_termination_notice(TerminationReason.MCP_DEGRADED_EMPTY, state=state)
    assert notice is not None
    assert notice.title == "The connection to Notion failed, so there's no reply."
    until = datetime(2026, 9, 28, 15, 42, tzinfo=UTC)
    rate = render_termination_notice(
        TerminationReason.RATE_LIMITED, state=TurnState(rate_limit_until=until)
    )
    assert rate is not None and rate.next_step == "Try again after 15:42 UTC."


def test_short_ref_and_fit_notice() -> None:
    assert short_ref("01a7c3qx2") == "7C3QX2"
    fitted = fit_notice(["x" * 5000, "y" * 5000], tail="Ref 7C3QX2", limit=3000)
    assert len(fitted) == 3000
    assert fitted.endswith("…\n\nRef 7C3QX2")
    assert fit_notice(["a", "b"], tail="Ref X", limit=100) == "a\n\nb\n\nRef X"


_NOUNS = RefusalNouns(scope="server", admin="a server admin", billing="`/billing`")


@pytest.mark.parametrize("reason", get_args(AdmissionDenialReason))
@pytest.mark.parametrize("in_dm", [False, True])
def test_refusals_have_two_spaced_lines(reason: AdmissionDenialReason, in_dm: bool) -> None:
    rendered = admission_refusal_text(reason, _NOUNS, bot_name="team-020-analyst", in_dm=in_dm)
    lines = rendered.split("\n\n")
    assert len(lines) == 2
    assert all(line.endswith(".") for line in lines)
    assert "{" not in rendered


def test_teams_setup_noun() -> None:
    nouns = RefusalNouns(
        scope="organisation", admin="an admin", billing="`billing`", setup="`setup`"
    )
    assert nouns.setup == "`setup`"


def test_dm_refusal_keeps_the_pinned_agent_separate_from_the_bot_name() -> None:
    channel = admission_refusal_text(
        "runs_elsewhere",
        _NOUNS,
        bot_name="daimon-staging",
        agent_name="team-020-analyst",
    )
    dm = admission_refusal_text(
        "runs_elsewhere",
        _NOUNS,
        bot_name="daimon-staging",
        agent_name="team-020-analyst",
        in_dm=True,
    )
    assert channel == "team-020-analyst only works in other channels.\n\nAsk it there."
    assert dm == "team-020-analyst only works in other channels.\n\nAsk it there."
