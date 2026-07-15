"""Tests for the pure embed state machine."""

from __future__ import annotations

from daimon.adapters.discord.embed import (
    EmbedEvent,
    EmbedState,
    TrailEntry,
    TurnPhase,
    _fmt_tokens,
    to_embed_data,
    to_preview_embed_data,
    update,
)
from daimon.adapters.discord.theme import (
    COLOR_GREEN,
    COLOR_RED,
    COLOR_THINKING,
    COLOR_TOOL_RUNNING,
)


def _make_state(
    phase: TurnPhase = TurnPhase.THINKING,
    trail: tuple[TrailEntry, ...] = (),
    agent_name: str = "test-agent",
    started_at: float = 0.0,
    usage_in: int = 0,
    usage_out: int = 0,
    cost_str: str | None = None,
) -> EmbedState:
    return EmbedState(
        phase=phase,
        trail=trail,
        agent_name=agent_name,
        started_at=started_at,
        usage_in=usage_in,
        usage_out=usage_out,
        cost_str=cost_str,
    )


class TestUpdate:
    def test_thinking_event_updates_phase_but_adds_no_trail_entry(self) -> None:
        # agent.thinking is a contentless progress ping (MA carries no thinking
        # text). The title reflects the phase; a trail line would be empty noise.
        state = _make_state()
        result = update(state, EmbedEvent(kind="thinking"))
        assert result.trail == (), "thinking must not add a trail entry"
        assert result.phase == TurnPhase.THINKING

    def test_thinking_event_preserves_existing_trail(self) -> None:
        state = _make_state(trail=(TrailEntry(emoji="⚙️", text="Bash"),))
        result = update(state, EmbedEvent(kind="thinking"))
        assert result.trail == (TrailEntry(emoji="⚙️", text="Bash"),), (
            "thinking must leave prior activity lines untouched"
        )
        assert result.phase == TurnPhase.THINKING

    def test_tool_use_event_transitions_to_tool_running(self) -> None:
        state = _make_state()
        result = update(state, EmbedEvent(kind="tool_use", label="Bash"))
        assert result.phase == TurnPhase.TOOL_RUNNING
        assert len(result.trail) == 1
        assert result.trail[0].emoji == "⚙️"
        assert result.trail[0].text == "Bash"

    def test_done_event_transitions_to_done(self) -> None:
        state = _make_state()
        result = update(state, EmbedEvent(kind="done"))
        assert result.phase == TurnPhase.DONE
        assert len(result.trail) == 1
        assert result.trail[0].emoji == "✅"
        assert result.trail[0].text == "complete"

    def test_error_event_transitions_to_error(self) -> None:
        state = _make_state()
        result = update(state, EmbedEvent(kind="error", label="upstream timeout"))
        assert result.phase == TurnPhase.ERROR
        assert len(result.trail) == 1
        assert result.trail[0].emoji == "❌"
        assert result.trail[0].text == "upstream timeout"

    def test_trail_limited_to_last_5_entries(self) -> None:
        state = _make_state()
        tools = ["tool_a", "tool_b", "tool_c", "tool_d", "tool_e", "tool_f", "tool_g"]
        for tool in tools:
            state = update(state, EmbedEvent(kind="tool_use", label=tool))
        assert len(state.trail) == 5
        # Last 5 entries should be tool_c through tool_g
        trail_texts = [e.text for e in state.trail]
        assert trail_texts == ["tool_c", "tool_d", "tool_e", "tool_f", "tool_g"]

    def test_multiple_tool_events_accumulate(self) -> None:
        state = _make_state()
        state = update(state, EmbedEvent(kind="tool_use", label="Read"))
        state = update(state, EmbedEvent(kind="tool_use", label="Write"))
        state = update(state, EmbedEvent(kind="tool_use", label="Bash"))
        assert len(state.trail) == 3
        assert state.trail[0].text == "Read"
        assert state.trail[1].text == "Write"
        assert state.trail[2].text == "Bash"

    def test_tool_use_shows_name_only_no_args(self) -> None:
        state = _make_state()
        result = update(state, EmbedEvent(kind="tool_use", label="Bash"))
        assert result.trail[0].text == "Bash"
        assert "(" not in result.trail[0].text
        assert ")" not in result.trail[0].text
        assert "=" not in result.trail[0].text

    def test_message_event_sets_text_preview_not_trail(self) -> None:
        state = _make_state()
        result = update(state, EmbedEvent(kind="message", label="I'll look that up for you"))
        assert result.trail == (), "messages render in the preview embed, not the trail"
        assert result.text_preview == "I'll look that up for you"
        assert result.phase == TurnPhase.THINKING

    def test_message_event_with_empty_label_keeps_previous_preview(self) -> None:
        state = _make_state()
        with_preview = update(state, EmbedEvent(kind="message", label="earlier reasoning"))
        result = update(with_preview, EmbedEvent(kind="message", label=""))
        assert result.text_preview == "earlier reasoning", "empty message must not clear preview"
        assert result.trail == ()

    def test_message_event_truncates_preview_at_250_chars(self) -> None:
        state = _make_state()
        result = update(state, EmbedEvent(kind="message", label="x" * 400))
        assert result.text_preview is not None
        assert len(result.text_preview) == 251, "250 chars + ellipsis"
        assert result.text_preview.endswith("…")

    def test_message_event_does_not_change_phase_from_thinking(self) -> None:
        state = _make_state(phase=TurnPhase.THINKING)
        result = update(state, EmbedEvent(kind="message", label="snippet"))
        assert result.phase == TurnPhase.THINKING


class TestToEmbedData:
    def test_thinking_phase_grey_color(self) -> None:
        state = _make_state(phase=TurnPhase.THINKING)
        data = to_embed_data(state)
        assert data.color == COLOR_THINKING
        assert data.color == 0x95A5A6

    def test_tool_running_phase_blue_color(self) -> None:
        state = _make_state(phase=TurnPhase.TOOL_RUNNING)
        data = to_embed_data(state)
        assert data.color == COLOR_TOOL_RUNNING
        assert data.color == 0x3498DB

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

    def test_description_joins_trail_entries(self) -> None:
        trail = (
            TrailEntry(emoji="\U0001f9e0", text="thinking"),
            TrailEntry(emoji="⚙️", text="Bash"),
            TrailEntry(emoji="✅", text="complete"),
        )
        state = _make_state(trail=trail)
        data = to_embed_data(state)
        lines = data.description.split("\n")
        assert len(lines) == 3
        assert lines[0] == "\U0001f9e0 thinking"
        assert lines[1] == "⚙️ Bash"
        assert lines[2] == "✅ complete"

    def test_footer_none_on_non_terminal(self) -> None:
        state = _make_state(phase=TurnPhase.THINKING)
        data = to_embed_data(state, now=100.0)
        assert data.footer is None

    def test_footer_set_on_done(self) -> None:
        state = _make_state(phase=TurnPhase.DONE, agent_name="my-agent", started_at=100.0)
        data = to_embed_data(state, now=105.0)
        assert data.footer is not None
        assert "5s" in data.footer
        assert "my-agent" in data.footer

    def test_footer_set_on_error(self) -> None:
        state = _make_state(phase=TurnPhase.ERROR, agent_name="my-agent", started_at=0.0)
        data = to_embed_data(state, now=12.0)
        assert data.footer is not None
        assert "12s" in data.footer
        assert "my-agent" in data.footer

    def test_footer_format_with_cost(self) -> None:
        state = _make_state(
            phase=TurnPhase.DONE,
            agent_name="Atlas",
            started_at=0.0,
            usage_in=1500,
            usage_out=320,
            cost_str="$0.04",
        )
        data = to_embed_data(state, now=12.0)
        # Centered dot U+00B7
        assert data.footer == "Atlas · 12s · 1.5k in / 320 out · $0.04", (
            "success footer is agent · elapsed · in/out · cost"
        )

    def test_footer_omits_cost_when_unpriced(self) -> None:
        state = _make_state(
            phase=TurnPhase.DONE,
            agent_name="Atlas",
            started_at=0.0,
            usage_in=1500,
            usage_out=320,
            cost_str=None,
        )
        data = to_embed_data(state, now=12.0)
        assert data.footer == "Atlas · 12s · 1.5k in / 320 out", (
            "unpriced model footer shows tokens only, no trailing cost segment"
        )

    def test_fmt_tokens_humanizes(self) -> None:
        assert _fmt_tokens(320) == "320", "sub-1000 counts render verbatim"
        assert _fmt_tokens(1500) == "1.5k", "1500 humanizes to 1.5k"
        assert _fmt_tokens(0) == "0", "zero renders as 0"
        assert _fmt_tokens(12000) == "12k", "whole-thousand strips trailing .0"

    def test_done_collapses_to_footer_only_no_checkmark(self) -> None:
        done = to_embed_data(
            _make_state(phase=TurnPhase.DONE, agent_name="Atlas", started_at=0.0), now=3.0
        )
        assert done.title == "", "done collapses to one line — no title, green bar signals success"
        assert done.description == "", "done collapses — the activity trail drops away"
        assert done.footer is not None and "✅" not in done.footer, (
            "the one-line summary lives in the footer; success path has no checkmark"
        )
        assert done.footer.startswith("Atlas · 3s · "), "footer leads with agent · elapsed"

    def test_error_collapses_to_footer_with_cross_and_reason(self) -> None:
        error = to_embed_data(_make_state(phase=TurnPhase.ERROR), now=3.0)
        assert error.title == "", "error also collapses to one line — no title"
        assert error.description == "", "error collapses — trail drops away"
        assert error.footer is not None and error.footer.startswith("❌ "), (
            "error keeps the ❌ in the footer (red bar + cross signal failure)"
        )

    def test_error_footer_renders_reason_and_summary(self) -> None:
        state = _make_state(
            phase=TurnPhase.ERROR,
            trail=(TrailEntry(emoji="❌", text="rate limited"),),
            agent_name="Atlas",
            started_at=0.0,
            usage_in=100,
            usage_out=50,
            cost_str=None,
        )
        data = to_embed_data(state, now=3.0)
        assert data.footer == "❌ rate limited · Atlas · 3s · 100 in / 50 out", (
            "error footer carries the failure reason then the data-bearing summary"
        )

    def test_update_carries_usage_fields_forward(self) -> None:
        state = _make_state(
            phase=TurnPhase.THINKING, usage_in=1500, usage_out=320, cost_str="$0.04"
        )
        next_state = update(state, EmbedEvent(kind="done"))
        assert next_state.usage_in == 1500, "update carries usage_in forward"
        assert next_state.usage_out == 320, "update carries usage_out forward"
        assert next_state.cost_str == "$0.04", "update carries cost_str forward"

    def test_title_contains_emoji_and_state_label(self) -> None:
        thinking = to_embed_data(_make_state(phase=TurnPhase.THINKING))
        assert "\U0001f9e0" in thinking.title
        assert "thinking" in thinking.title

        tool_running = to_embed_data(_make_state(phase=TurnPhase.TOOL_RUNNING))
        assert "⚙️" in tool_running.title
        assert "tool" in tool_running.title

        # Terminal phases collapse — no title (the summary moves to the footer).
        done = to_embed_data(_make_state(phase=TurnPhase.DONE))
        assert done.title == "", "done collapses to footer-only — no title"

        error = to_embed_data(_make_state(phase=TurnPhase.ERROR))
        assert error.title == "", "error collapses to footer-only — no title"


class TestElapsedLine:
    def test_in_progress_with_now_shows_elapsed_first_line(self) -> None:
        state = _make_state(phase=TurnPhase.THINKING, started_at=100.0)
        data = to_embed_data(state, now=142.0)
        assert data.description.splitlines()[0] == "⏱️ 42s", (
            "elapsed line should lead the activity embed"
        )

    def test_elapsed_formats_minutes_and_seconds(self) -> None:
        state = _make_state(phase=TurnPhase.THINKING, started_at=0.0)
        state = EmbedState(phase=TurnPhase.THINKING, started_at=1.0)
        data = to_embed_data(state, now=124.0)
        assert data.description.splitlines()[0] == "⏱️ 2m 3s"

    def test_in_progress_without_now_has_no_elapsed_line(self) -> None:
        state = _make_state(
            phase=TurnPhase.TOOL_RUNNING,
            trail=(TrailEntry(emoji="⚙️", text="Bash"),),
            started_at=100.0,
        )
        data = to_embed_data(state)
        assert data.description == "⚙️ Bash", "no now → trail only, no elapsed line"


class TestPreviewEmbed:
    def test_no_preview_returns_none(self) -> None:
        state = _make_state()
        assert to_preview_embed_data(state) is None, "no message text yet → no preview embed"

    def test_preview_renders_message_text_with_speech_emoji(self) -> None:
        state = update(_make_state(), EmbedEvent(kind="message", label="Let me check the data"))
        data = to_preview_embed_data(state)
        assert data is not None
        assert data.description == "💬 Let me check the data"
        assert data.footer is None

    def test_preview_escapes_markdown(self) -> None:
        state = update(_make_state(), EmbedEvent(kind="message", label="use `code` and *stars*"))
        data = to_preview_embed_data(state)
        assert data is not None
        assert "\\`code\\`" in data.description, (
            "backticks escaped so truncation can't break markup"
        )
        assert "\\*stars\\*" in data.description

    def test_preview_color_matches_phase(self) -> None:
        state = update(_make_state(), EmbedEvent(kind="message", label="reasoning"))
        state = update(state, EmbedEvent(kind="tool_use", label="Bash"))
        data = to_preview_embed_data(state)
        assert data is not None
        assert data.color == COLOR_TOOL_RUNNING, "preview bar color follows the activity phase"

    def test_preview_survives_tool_and_thinking_events(self) -> None:
        state = update(_make_state(), EmbedEvent(kind="message", label="the reasoning so far"))
        state = update(state, EmbedEvent(kind="tool_use", label="Bash"))
        state = update(state, EmbedEvent(kind="thinking", label=""))
        assert state.text_preview == "the reasoning so far", (
            "preview persists until the next message replaces it"
        )
