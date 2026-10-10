"""Tests for the pure embed state machine."""

from __future__ import annotations

import dataclasses
from typing import Literal

from daimon.adapters.discord.embed import (
    EmbedEvent,
    EmbedState,
    TurnPhase,
    to_embed_data,
    update,
    update_activity,
)
from daimon.adapters.discord.lifecycle import build_discord_embed
from daimon.adapters.discord.theme import (
    COLOR_GREEN,
    COLOR_IN_PROGRESS,
    COLOR_RED,
)
from daimon.core.turn.state import ToolUseBlock, TurnState


def _make_state(
    phase: TurnPhase = TurnPhase.THINKING,
    agent_name: str = "test-agent",
    started_at: float = 0.0,
    cost_str: str | None = None,
) -> EmbedState:
    return EmbedState(
        phase=phase,
        agent_name=agent_name,
        started_at=started_at,
        cost_str=cost_str,
    )


def _call(name: str, status: Literal["pending", "complete", "failed"] = "pending") -> ToolUseBlock:
    return ToolUseBlock(
        kind="tool_use", id=f"tu_{name}", type="agent.tool_use", name=name, input={}, status=status
    )


def test_max_length_error_notice_keeps_the_summary_line() -> None:
    tail = "\n**Next:** Add credit.\n`rid: test`"
    notice = "x" * (950 - len(tail)) + tail
    assert len(notice) == 950
    state = dataclasses.replace(
        _make_state(phase=TurnPhase.ERROR, cost_str="$0.05"),
        notice=notice,
        balance_str="$9.95",
    )
    embed = build_discord_embed(to_embed_data(state, now=12.0))

    assert embed.description == "Add credit."
    assert embed.fields[0].value == notice.replace("**Next:** Add credit.\n", "")
    assert len(embed.fields) == 1, "the numbers ride the footer, not a Details field"
    assert embed.footer.text == "test-agent\u2003\u200312s\u2003\u2003$0.05 used"


def test_empty_notice_next_step_keeps_retry_text_and_metrics() -> None:
    state = dataclasses.replace(
        _make_state(phase=TurnPhase.ERROR, cost_str="$0.05"),
        notice="Model usage limit reached.\n**Next:** \n`rid: test`",
        balance_str="$9.95",
    )
    embed = build_discord_embed(to_embed_data(state, now=12.0))

    assert embed.description == "Mention me to try again."
    assert "Model usage limit reached." in embed.fields[0].value
    assert "`rid: test`" in embed.fields[0].value
    assert embed.footer.text is not None
    assert "$0.05 used" in embed.footer.text
    assert "$9.95" not in embed.footer.text


def test_a_long_agent_name_gives_way_so_the_footer_fits() -> None:
    state = dataclasses.replace(
        _make_state(phase=TurnPhase.DONE, agent_name="a" * 2040, cost_str="$0.042"),
        balance_str="$41.20 left",
    )
    embed = build_discord_embed(to_embed_data(state, now=12.0))

    assert embed.footer.text is not None
    assert len(embed.footer.text) == 2048, "Discord rejects a footer over 2,048 characters"
    assert embed.footer.text.endswith("…\u2003\u200312s\u2003\u2003$0.042 used")


class TestUpdate:
    def test_message_event_sets_draft_without_touching_phase(self) -> None:
        state = _make_state(phase=TurnPhase.TOOL_RUNNING)
        result = update(state, EmbedEvent(kind="message", label="I'll look that up for you"))
        assert result.text_preview == "I'll look that up for you", "message text becomes the draft"
        assert result.phase == TurnPhase.TOOL_RUNNING, "the phase comes from the turn state only"

    def test_message_event_with_empty_label_keeps_previous_draft(self) -> None:
        with_draft = update(_make_state(), EmbedEvent(kind="message", label="earlier reasoning"))
        result = update(with_draft, EmbedEvent(kind="message", label=""))
        assert result.text_preview == "earlier reasoning", "empty message must not clear the draft"

    def test_message_event_clips_draft_at_300_chars(self) -> None:
        result = update(_make_state(), EmbedEvent(kind="message", label="x" * 400))
        assert result.text_preview is not None
        assert len(result.text_preview) == 301, "300 chars + ellipsis"
        assert result.text_preview.endswith("…")

    def test_done_event_transitions_to_done(self) -> None:
        result = update(_make_state(), EmbedEvent(kind="done"))
        assert result.phase == TurnPhase.DONE, "done moves the card to its terminal phase"

    def test_error_event_records_reason(self) -> None:
        result = update(_make_state(), EmbedEvent(kind="error", label="upstream timeout"))
        assert result.phase == TurnPhase.ERROR, "error moves the card to its terminal phase"
        assert result.error_reason == "upstream timeout", "the label is the footer's reason"


class TestUpdateActivity:
    def test_running_call_reads_as_working(self) -> None:
        result = update_activity(
            _make_state(), TurnState(content=[_call("bash", "complete"), _call("read")])
        )
        assert result.phase == TurnPhase.TOOL_RUNNING, "a pending call means the turn is working"
        assert result.tool_lines == ("✔️ Ran a command", "🔍 Reading a file"), (
            "tool lines come from the turn state"
        )

    def test_no_running_call_reads_as_thinking(self) -> None:
        state = _make_state(phase=TurnPhase.TOOL_RUNNING)
        result = update_activity(state, TurnState(content=[_call("bash", "complete")]))
        assert result.phase == TurnPhase.THINKING, "once every call finished the turn is thinking"

    def test_terminal_state_is_left_alone(self) -> None:
        done = _make_state(phase=TurnPhase.DONE)
        assert update_activity(done, TurnState(content=[_call("bash")])) is done, (
            "a late render must not reopen a finished card"
        )


class TestToEmbedData:
    def test_thinking_and_working_share_one_blue_color(self) -> None:
        for phase in (TurnPhase.THINKING, TurnPhase.TOOL_RUNNING):
            data = to_embed_data(_make_state(phase=phase))
            assert data.color == COLOR_IN_PROGRESS == 0x3498DB, (
                f"{phase} is in progress; the headline word, not the bar, tells the two apart"
            )

    def test_done_phase_green_color(self) -> None:
        state = _make_state(phase=TurnPhase.DONE)
        data = to_embed_data(state)
        assert data.color == COLOR_GREEN
        assert data.color == 0x57F287

    def test_error_phase_red_color(self) -> None:
        state = _make_state(phase=TurnPhase.ERROR)
        data = to_embed_data(state)
        assert data.color == COLOR_RED
        assert data.color == 0xED4245

    def test_public_progress_keeps_status_without_tools_or_draft(self) -> None:
        state = dataclasses.replace(
            _make_state(phase=TurnPhase.TOOL_RUNNING, started_at=100.0),
            tool_lines=("✔️ Ran a command", "🔍 Reading a file"),
            text_preview="Checking *the* logs",
        )
        data = to_embed_data(state, now=165.0)
        assert data.title == "Working on it…"
        assert data.description == ""
        assert data.details is None

    def test_tool_diagnostics_are_kept_out_of_public_card(self) -> None:
        state = dataclasses.replace(
            _make_state(phase=TurnPhase.TOOL_RUNNING),
            tool_lines=("internal_tool sesn_private agent_private rid: private",),
            text_preview="internal_tool sesn_private agent_private rid: private",
        )
        data = to_embed_data(state)
        assert data.details is None
        assert data.description == ""
        assert "private" not in repr(data)

    def test_footer_none_on_non_terminal(self) -> None:
        state = _make_state(phase=TurnPhase.THINKING)
        data = to_embed_data(state, now=100.0)
        assert data.footer is None

    def test_footer_set_on_done(self) -> None:
        state = _make_state(phase=TurnPhase.DONE, agent_name="my-agent", started_at=100.0)
        data = to_embed_data(state, now=105.0)
        assert data.footer == "my-agent\u2003\u20035s"

    def test_footer_set_on_error(self) -> None:
        state = _make_state(phase=TurnPhase.ERROR, agent_name="my-agent", started_at=0.0)
        data = to_embed_data(state, now=12.0)
        assert data.footer == "my-agent\u2003\u200312s"

    def test_footer_format_with_cost(self) -> None:
        state = dataclasses.replace(
            _make_state(
                phase=TurnPhase.DONE,
                agent_name="Atlas",
                started_at=0.0,
                cost_str="$0.04",
            ),
            balance_str="$12.50 left",
        )
        data = to_embed_data(state, now=12.0)
        assert data.footer == "Atlas\u2003\u200312s\u2003\u2003$0.04 used", (
            "the shared one-line format retains cost without public balance or tokens"
        )
        assert data.details is None, "no Details field on a finished card"

    def test_footer_omits_cost_when_unpriced(self) -> None:
        state = _make_state(
            phase=TurnPhase.DONE,
            agent_name="Atlas",
            started_at=0.0,
            cost_str=None,
        )
        data = to_embed_data(state, now=12.0)
        assert data.footer == "Atlas\u2003\u200312s", "an unpriced turn drops the cost and its gap"

    def test_done_collapses_to_footer_only_no_checkmark(self) -> None:
        done = to_embed_data(
            _make_state(phase=TurnPhase.DONE, agent_name="Atlas", started_at=0.0), now=3.0
        )
        assert done.title == "", "done collapses to one line — no title, green bar signals success"
        assert done.description == "", "done collapses — the activity trail drops away"
        assert done.footer is not None and "✅" not in done.footer, (
            "the one-line summary lives in the footer; success path has no checkmark"
        )
        assert done.footer == "Atlas\u2003\u20033s"
        assert done.details is None

    def test_error_collapses_to_footer_with_cross_and_reason(self) -> None:
        error = to_embed_data(_make_state(phase=TurnPhase.ERROR), now=3.0)
        assert error.title == "Something went wrong."
        assert error.description == "Mention me to try again."
        assert error.footer == "test-agent\u2003\u20033s"
        assert error.details is None

    def test_error_footer_renders_reason_and_summary(self) -> None:
        state = _make_state(
            phase=TurnPhase.ERROR,
            agent_name="Atlas",
            started_at=0.0,
            cost_str=None,
        )
        data = to_embed_data(dataclasses.replace(state, error_reason="rate limited"), now=3.0)
        assert data.footer == "Atlas\u2003\u20033s"

    def test_update_carries_cost_forward(self) -> None:
        state = _make_state(phase=TurnPhase.THINKING, cost_str="$0.04")
        next_state = update(state, EmbedEvent(kind="done"))
        assert next_state.cost_str == "$0.04", "update carries cost_str forward"

    def test_terminal_phases_have_no_title(self) -> None:
        done = to_embed_data(_make_state(phase=TurnPhase.DONE))
        assert done.title == "", "done collapses to footer-only — no title"

        error = to_embed_data(_make_state(phase=TurnPhase.ERROR))
        assert error.title == "Something went wrong."


class TestHeadline:
    def test_in_progress_with_now_leads_with_state_and_elapsed(self) -> None:
        data = to_embed_data(_make_state(started_at=100.0), now=142.0)
        assert data.title == "Working on it…", "the headline leads the card"

    def test_headline_runs_to_hours(self) -> None:
        state = _make_state(phase=TurnPhase.TOOL_RUNNING, started_at=1.0)
        data = to_embed_data(state, now=1.0 + 2 * 3600 + 3 * 60)
        assert data.title == "Working on it…"

    def test_in_progress_without_now_has_no_elapsed(self) -> None:
        data = to_embed_data(_make_state(started_at=100.0))
        assert data.title == "Working on it…"


def test_public_connection_failure_omits_server_names() -> None:
    from daimon.adapters.discord.embed import format_termination_notice
    from daimon.core.turn.notices import TerminationNotice
    from daimon.core.turn.termination import TerminationReason

    notice = TerminationNotice(
        reason=TerminationReason.MCP_DEGRADED_EMPTY,
        headline="Tool connection failed",
        cause="internal_server agent_private sesn_private could not connect",
        survived="The conversation is kept.",
        next_step="Ask an admin to check the connection.",
    )
    text = format_termination_notice(notice)
    assert "A connected service failed" in text
    assert "private" not in text
    assert "internal_server" not in text
