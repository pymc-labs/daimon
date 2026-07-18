"""Tests for the Block Kit pure state machine (blockkit.py).

Tasks 2 and 3.

Task 2: state machine (TurnPhase / EmbedEvent / TrailEntry / State / update)
Task 3: to_blocks renderer (emoji title, elapsed/trail context, preview, cancel
        button, terminal collapse with cost footer, no color anywhere)

Mirrors discord/tests/test_embed.py structure with the _make_state helper
pattern. No DB required — pure stdlib-only module.
"""

from __future__ import annotations

from typing import Any

from daimon.adapters.slack.blockkit import (
    EmbedEvent,
    State,
    TrailEntry,
    TurnPhase,
    _fmt_tokens,
    to_blocks,
    update,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_state(
    phase: TurnPhase = TurnPhase.THINKING,
    trail: tuple[TrailEntry, ...] = (),
    agent_name: str = "test-agent",
    started_at: float = 0.0,
    usage_in: int = 0,
    usage_out: int = 0,
    cost_str: str | None = None,
    text_preview: str | None = None,
) -> State:
    return State(
        phase=phase,
        trail=trail,
        agent_name=agent_name,
        started_at=started_at,
        usage_in=usage_in,
        usage_out=usage_out,
        cost_str=cost_str,
        text_preview=text_preview,
    )


# ---------------------------------------------------------------------------
# Task 2: update() state machine
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_thinking_event_updates_phase_but_adds_no_trail_entry(self) -> None:
        """agent.thinking is a contentless progress ping — adds no trail entry."""
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

    def test_tool_use_event_transitions_to_tool_running_and_appends_trail(self) -> None:
        state = _make_state()
        result = update(state, EmbedEvent(kind="tool_use", label="search"))
        assert result.phase == TurnPhase.TOOL_RUNNING
        assert len(result.trail) == 1
        assert result.trail[0].emoji == "⚙️"
        assert result.trail[0].text == "search"

    def test_six_tool_use_events_caps_trail_at_5_keeping_last_5(self) -> None:
        """Feeding 6 tool_use events caps trail at 5, keeping the most recent."""
        state = _make_state()
        tools = ["a", "b", "c", "d", "e", "f"]
        for t in tools:
            state = update(state, EmbedEvent(kind="tool_use", label=t))
        assert len(state.trail) == 5, "trail is capped at 5 entries"
        trail_texts = [e.text for e in state.trail]
        assert trail_texts == ["b", "c", "d", "e", "f"], (
            "trail keeps the last 5, discarding the oldest"
        )

    def test_message_event_sets_text_preview_and_does_not_add_trail_entry(self) -> None:
        state = _make_state()
        result = update(state, EmbedEvent(kind="message", label="hello there"))
        assert result.trail == (), "messages go to text_preview, not the trail"
        assert result.text_preview == "hello there"

    def test_message_event_with_300_char_label_caps_preview_at_250_plus_ellipsis(
        self,
    ) -> None:
        """A 300-char message label is capped to 250 chars + '…'."""
        state = _make_state()
        long_label = "x" * 300
        result = update(state, EmbedEvent(kind="message", label=long_label))
        assert result.text_preview is not None
        assert len(result.text_preview) == 251, "250 chars + ellipsis == 251 total"
        assert result.text_preview.endswith("…")

    def test_message_event_with_empty_label_only_changes_phase(self) -> None:
        """An empty message label only updates the phase; preview is unchanged."""
        state = _make_state(text_preview="prior preview")
        result = update(state, EmbedEvent(kind="message", label=""))
        assert result.text_preview == "prior preview", (
            "empty message label must not clear or change the preview"
        )

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


# ---------------------------------------------------------------------------
# Task 3: to_blocks renderer
# ---------------------------------------------------------------------------


def _find_blocks_by_type(blocks: list[dict[str, Any]], block_type: str) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("type") == block_type]


def _find_action_ids(blocks: list[dict[str, Any]]) -> list[str]:
    """Collect all action_id values from all elements across all actions blocks."""
    ids: list[str] = []
    for b in blocks:
        if b.get("type") == "actions":
            for elem in b.get("elements", []):
                if "action_id" in elem:
                    ids.append(elem["action_id"])
    return ids


class TestToBlocks:
    def test_running_thinking_state_has_section_with_brain_emoji(self) -> None:
        """THINKING state produces a section block whose text starts with the brain emoji."""
        state = _make_state(phase=TurnPhase.THINKING)
        blocks = to_blocks(state, now=None)
        sections = _find_blocks_by_type(blocks, "section")
        assert sections, "to_blocks must emit at least one section block"
        title_text = sections[0]["text"]["text"]
        assert "\U0001f9e0" in title_text, "THINKING section text must contain the brain emoji"

    def test_running_state_has_cancel_button_with_correct_action_id(self) -> None:
        """While a turn runs, an actions block with action_id='cancel_turn' is present."""
        state = _make_state(phase=TurnPhase.THINKING)
        blocks = to_blocks(state, now=None)
        action_ids = _find_action_ids(blocks)
        assert "cancel_turn" in action_ids, (
            "running turn must have a cancel button with action_id='cancel_turn'"
        )

    def test_running_state_with_trail_has_context_block_with_trail_entries(self) -> None:
        """A running state with trail entries produces a context block containing them."""
        trail = (
            TrailEntry(emoji="⚙️", text="search"),
            TrailEntry(emoji="⚙️", text="read"),
        )
        state = _make_state(phase=TurnPhase.TOOL_RUNNING, trail=trail, started_at=1.0)
        blocks = to_blocks(state, now=6.0)
        context_blocks = _find_blocks_by_type(blocks, "context")
        assert context_blocks, "trail must produce a context block"
        context_text = context_blocks[0]["elements"][0]["text"]
        assert "search" in context_text, "trail entry 'search' must appear in context"
        assert "read" in context_text, "trail entry 'read' must appear in context"
        assert "⏱️" in context_text, "context block must contain elapsed marker"

    def test_running_state_with_text_preview_has_section_with_escaped_preview(
        self,
    ) -> None:
        """text_preview with < must appear as &lt; in the section block text."""
        state = _make_state(phase=TurnPhase.THINKING, text_preview="result < expected")
        blocks = to_blocks(state, now=None)
        # Find section blocks whose text contains the preview marker
        preview_sections = [
            b for b in _find_blocks_by_type(blocks, "section") if "💬" in b["text"]["text"]
        ]
        assert preview_sections, "text_preview must produce a section block with 💬"
        preview_text = preview_sections[0]["text"]["text"]
        assert "&lt;" in preview_text, "< in preview must be entity-escaped to &lt;"
        assert "<" not in preview_text.replace("&lt;", ""), (
            "raw < must not appear in the escaped preview"
        )

    def test_done_state_has_no_cancel_button(self) -> None:
        """DONE (terminal) state must not have an actions block."""
        state = _make_state(phase=TurnPhase.DONE, agent_name="bot", started_at=0.0)
        blocks = to_blocks(state, now=5.0)
        action_ids = _find_action_ids(blocks)
        assert "cancel_turn" not in action_ids, "terminal (DONE) turn must not have a cancel button"

    def test_done_state_has_cost_footer_context_block(self) -> None:
        """DONE state produces a trailing context block with the cost/usage summary."""
        state = _make_state(
            phase=TurnPhase.DONE,
            agent_name="Atlas",
            started_at=0.0,
            usage_in=1500,
            usage_out=320,
            cost_str="$0.04",
        )
        blocks = to_blocks(state, now=12.0)
        context_blocks = _find_blocks_by_type(blocks, "context")
        assert context_blocks, "DONE state must produce a context block"
        summary_text = context_blocks[-1]["elements"][0]["text"]
        assert "Atlas" in summary_text, "footer must contain agent_name"
        assert "12s" in summary_text, "footer must contain elapsed time"
        assert "$0.04" in summary_text, "footer must contain cost_str when set"

    def test_error_state_summary_context_has_cross_emoji_and_reason(self) -> None:
        """ERROR state's summary context block carries the cross emoji + last trail text."""
        trail = (TrailEntry(emoji="❌", text="rate limited"),)
        state = _make_state(
            phase=TurnPhase.ERROR,
            trail=trail,
            agent_name="Atlas",
            started_at=0.0,
        )
        blocks = to_blocks(state, now=5.0)
        context_blocks = _find_blocks_by_type(blocks, "context")
        assert context_blocks, "ERROR state must produce a context block"
        summary_text = context_blocks[-1]["elements"][0]["text"]
        assert "❌" in summary_text, "error summary must contain the cross emoji"
        assert "rate limited" in summary_text, (
            "error summary must contain the last trail entry text as the reason"
        )

    def test_no_block_contains_color_key(self) -> None:
        """No block dict anywhere must contain a 'color' key."""
        state = _make_state(
            phase=TurnPhase.THINKING,
            trail=(TrailEntry(emoji="⚙️", text="tool"),),
            text_preview="preview",
        )
        blocks = to_blocks(state, now=5.0)
        for block in blocks:
            assert "color" not in block, f"block {block!r} must not contain a 'color' key"

    def test_fmt_tokens_humanizes(self) -> None:
        assert _fmt_tokens(320) == "320", "sub-1000 counts render verbatim"
        assert _fmt_tokens(1500) == "1.5k", "1500 humanizes to 1.5k"
        assert _fmt_tokens(0) == "0", "zero renders as 0"
        assert _fmt_tokens(12000) == "12k", "whole-thousand strips trailing .0"
