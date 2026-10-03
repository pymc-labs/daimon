"""The termination notice: one explanation per reason, platform-neutral."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest
from daimon.core.turn import TerminationReason, render_termination_notice
from daimon.core.turn.errors import AdmissionDenialReason
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text, fit_notice
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


def _many_failures(count: int, length: int) -> tuple[McpServerFailure, ...]:
    return tuple(
        McpServerFailure(
            server_name=f"{i:02d}" + "s" * (length - 2),
            error_type="mcp_connection_failed_error",
            message="down",
            retry_status="exhausted",
        )
        for i in range(count)
    )


def test_many_long_server_names_stay_bounded() -> None:
    """The reviewer's case: 45 failed servers with 100-character names."""
    state = TurnState(
        mcp_failures=_many_failures(45, 100),
        content=[_tool("t" * 500 + str(i), "pending") for i in range(45)],
    )

    notice = render_termination_notice(
        TerminationReason.MCP_DEGRADED_EMPTY, state=state, request_id="01RID"
    )

    assert notice is not None
    assert "and 42 more" in notice.cause
    work = notice.work_line()
    assert work is not None and "and 40 more" in work
    assert len(notice.plain_text()) < 1000


def test_fit_notice_clips_the_body_and_keeps_the_request_id() -> None:
    fitted = fit_notice(["x" * 5000, "y" * 5000], tail="`rid: 01RID`", limit=3000)

    assert len(fitted) == 3000
    assert fitted.endswith("…\n`rid: 01RID`")


def test_fit_notice_leaves_short_notices_alone() -> None:
    assert fit_notice(["a", "b"], tail="rid", limit=100) == "a\nb\nrid"
    assert fit_notice(["a" * 10], tail=None, limit=5) == "aaaa…"


def test_several_failed_servers_read_as_plural() -> None:
    state = TurnState(mcp_failures=_many_failures(2, 6))
    notice = render_termination_notice(TerminationReason.MCP_DEGRADED_EMPTY, state=state)
    assert notice is not None
    assert notice.cause.startswith("The tool servers 00ssss, 01ssss failed")
    assert notice.cause.endswith("without them.")


_NOUNS = RefusalNouns(scope="server", admin="a server admin", billing="`/billing`")


@pytest.mark.parametrize("in_dm", [False, True])
@pytest.mark.parametrize("reason", get_args(AdmissionDenialReason))
def test_every_admission_refusal_is_a_sentence_not_a_code(
    reason: AdmissionDenialReason, in_dm: bool
) -> None:
    text = admission_refusal_text(reason, _NOUNS, bot_name="daimon", in_dm=in_dm)

    assert reason not in text, f"the raw reason leaked: {text!r}"
    assert text[0].isupper() and text.endswith("."), f"not a sentence: {text!r}"
    assert "{" not in text, f"a placeholder was left unfilled: {text!r}"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            "balance_depleted",
            "This server's daimon credit is depleted. A server admin can top up with `/billing`.",
        ),
        ("cap_exceeded", "You've reached your monthly usage cap. An operator can raise it."),
        (
            "channel_budget_exceeded",
            "This channel has used its spending budget. A server admin can raise or clear it.",
        ),
        (
            "invoker_not_allowed",
            "You aren't on this server's list of people who can start a turn. "
            "A server admin can add you.",
        ),
        (
            "runs_elsewhere",
            "This agent's rule runs it only in other channels, so it can't answer here.",
        ),
        (
            "own_agents_only",
            "This channel is kept to its own agents and the one that would answer isn't one of "
            "them. A server admin must set the channel's agent.",
        ),
        ("writers_none", "This channel's rule lets nobody write in it, so the agent can't answer."),
        (
            "external_participant",
            "People from another organisation can use this agent only in a channel kept to its "
            "own agents.",
        ),
    ],
)
def test_admission_refusal_words_each_reason_in_the_platform_nouns(
    reason: AdmissionDenialReason, expected: str
) -> None:
    assert admission_refusal_text(reason, _NOUNS, bot_name="daimon") == expected, reason


def test_admission_refusal_without_a_bot_name_reads_naturally() -> None:
    nouns = RefusalNouns(
        scope="organisation", admin="an admin", billing="`billing` in a 1:1 chat with me"
    )
    text = admission_refusal_text("balance_depleted", nouns)

    assert text == (
        "This organisation's credit is depleted. "
        "An admin can top up with `billing` in a 1:1 chat with me."
    ), text


@pytest.mark.parametrize(
    ("reason", "word"),
    [
        ("runs_elsewhere", "rule running it"),
        ("own_agents_only", "kept to its own agents"),
        ("writers_none", "nobody write"),
        ("external_participant", "organisation"),
    ],
)
def test_a_place_bound_refusal_in_a_dm_says_why_it_cannot_move(
    reason: AdmissionDenialReason, word: str
) -> None:
    text = admission_refusal_text(reason, _NOUNS, in_dm=True)

    assert word in text and text.endswith("a DM."), text


def test_a_dm_refusal_not_bound_to_the_place_reads_as_in_the_channel() -> None:
    in_dm = admission_refusal_text("channel_budget_exceeded", _NOUNS, in_dm=True)

    assert in_dm == admission_refusal_text("channel_budget_exceeded", _NOUNS), in_dm


def test_the_cap_notice_says_the_cap_is_the_persons_and_an_operator_raises_it() -> None:
    """The cap is per person and only the operator raises it, as the refusal says."""
    notice = render_termination_notice(TerminationReason.ADMISSION_CAP_EXCEEDED)
    assert notice is not None
    assert notice.cause.startswith("You've reached your monthly usage cap"), notice.cause
    assert notice.next_step == "An operator can raise it.", "no admin command raises a cap"
