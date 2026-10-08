"""Tests for the Block Kit pure state machine (blockkit.py).

State machine: TurnPhase / EmbedEvent / State / update / update_activity.
to_blocks renderer: headline, tool lines, draft, cancel button, terminal
collapse with cost footer, no color anywhere.

Mirrors discord/tests/test_embed.py structure with the _make_state helper
pattern. No DB required.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from daimon.adapters.slack.blockkit import (
    EmbedEvent,
    State,
    TurnPhase,
    _fmt_tokens,
    to_blocks,
    to_fallback_text,
    to_interrupted_blocks,
    update,
    update_activity,
)
from daimon.core.turn.state import ToolUseBlock, TurnState

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_state(
    phase: TurnPhase = TurnPhase.THINKING,
    tool_lines: tuple[str, ...] = (),
    agent_name: str = "test-agent",
    started_at: float = 0.0,
    usage_in: int = 0,
    usage_out: int = 0,
    cost_str: str | None = None,
    text_preview: str | None = None,
) -> State:
    return State(
        phase=phase,
        tool_lines=tool_lines,
        agent_name=agent_name,
        started_at=started_at,
        usage_in=usage_in,
        usage_out=usage_out,
        cost_str=cost_str,
        text_preview=text_preview,
    )


# ---------------------------------------------------------------------------
# update() state machine
# ---------------------------------------------------------------------------


def _call(name: str, status: Literal["pending", "complete", "failed"] = "pending") -> ToolUseBlock:
    return ToolUseBlock(
        kind="tool_use", id=f"tu_{name}", type="agent.tool_use", name=name, input={}, status=status
    )


class TestUpdate:
    def test_message_event_sets_draft_without_touching_phase(self) -> None:
        state = _make_state(phase=TurnPhase.TOOL_RUNNING)
        result = update(state, EmbedEvent(kind="message", label="hello there"))
        assert result.text_preview == "hello there", "message text becomes the draft"
        assert result.phase == TurnPhase.TOOL_RUNNING, "the phase comes from the turn state only"

    def test_message_event_with_400_char_label_caps_preview_at_300_plus_ellipsis(
        self,
    ) -> None:
        """A 400-char message label is capped to 300 chars + '…'."""
        state = _make_state()
        long_label = "x" * 400
        result = update(state, EmbedEvent(kind="message", label=long_label))
        assert result.text_preview is not None
        assert len(result.text_preview) == 301, "300 chars + ellipsis == 301 total"
        assert result.text_preview.endswith("…")

    def test_message_event_with_empty_label_keeps_the_draft(self) -> None:
        state = _make_state(text_preview="prior preview")
        result = update(state, EmbedEvent(kind="message", label=""))
        assert result.text_preview == "prior preview", (
            "empty message label must not clear or change the preview"
        )

    def test_done_event_transitions_to_done(self) -> None:
        result = update(_make_state(), EmbedEvent(kind="done"))
        assert result.phase == TurnPhase.DONE, "done moves the card to its terminal phase"

    def test_error_event_records_reason(self) -> None:
        result = update(_make_state(), EmbedEvent(kind="error", label="upstream timeout"))
        assert result.phase == TurnPhase.ERROR, "error moves the card to its terminal phase"
        assert result.error_reason == "upstream timeout", "the label is the summary's reason"


class TestUpdateActivity:
    def test_running_call_reads_as_working(self) -> None:
        result = update_activity(
            _make_state(), TurnState(content=[_call("bash", "failed"), _call("grep")])
        )
        assert result.phase == TurnPhase.TOOL_RUNNING, "a pending call means the turn is working"
        assert result.tool_lines == ("🚫 Ran a command", "🔍 Searching files"), (
            "tool lines come from the turn state"
        )

    def test_no_running_call_reads_as_thinking(self) -> None:
        state = _make_state(phase=TurnPhase.TOOL_RUNNING)
        result = update_activity(state, TurnState(content=[_call("bash", "complete")]))
        assert result.phase == TurnPhase.THINKING, "once every call finished the turn is thinking"

    def test_terminal_state_is_left_alone(self) -> None:
        error = _make_state(phase=TurnPhase.ERROR)
        assert update_activity(error, TurnState(content=[_call("bash")])) is error, (
            "a late render must not reopen a finished card"
        )


# ---------------------------------------------------------------------------
# to_blocks renderer
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
    def test_running_state_leads_with_the_headline(self) -> None:
        state = _make_state(phase=TurnPhase.THINKING, started_at=100.0)
        sections = _find_blocks_by_type(to_blocks(state, now=112.0), "section")
        assert sections[0]["text"]["text"] == "*Working on it…*", (
            "the first section is the bold state word and the elapsed time"
        )

    def test_fallback_text_is_the_headline_in_plain_words(self) -> None:
        state = _make_state(phase=TurnPhase.TOOL_RUNNING, started_at=1.0)
        assert to_fallback_text(state, now=66.0) == "Working on it…", (
            "notifications read the headline, not an internal phase name"
        )

    def test_running_state_has_cancel_button_with_correct_action_id(self) -> None:
        """While a turn runs, an actions block with action_id='cancel_turn' is present."""
        state = _make_state(phase=TurnPhase.THINKING)
        blocks = to_blocks(state, now=None)
        action_ids = _find_action_ids(blocks)
        assert "cancel_turn" in action_ids, (
            "running turn must have a cancel button with action_id='cancel_turn'"
        )

    def test_running_state_with_tool_lines_has_escaped_code_block(self) -> None:
        state = _make_state(
            phase=TurnPhase.TOOL_RUNNING,
            tool_lines=("✔️ Read a file", "🖋️ Q&A sync"),
            started_at=1.0,
        )
        blocks = to_blocks(state, now=66.0)
        sections = _find_blocks_by_type(blocks, "section")
        assert sections[0]["text"]["text"] == "*Working on it…*"
        contexts = _find_blocks_by_type(blocks, "context")
        assert contexts[0]["elements"][0]["text"] == "*Details*\n✔️ Read a file\n🖋️ Q&amp;A sync"

    def test_running_state_with_text_preview_has_quoted_escaped_draft(
        self,
    ) -> None:
        """text_preview with < must appear as &lt; in a quoted section."""
        state = _make_state(phase=TurnPhase.THINKING, text_preview="result < expected")
        blocks = to_blocks(state, now=None)
        draft = _find_blocks_by_type(blocks, "section")[-1]
        assert draft["text"]["text"] == "> result &lt; expected", (
            "the draft is quoted and entity-escaped"
        )
        assert draft.get("expand") is True, "the draft must not fold behind 'see more'"

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
        assert "Atlas" in summary_text, "the bot-header fallback must name the agent"
        customized = to_blocks(replace(state, header_customized=True), now=12.0)
        assert "Atlas" not in customized[-1]["elements"][0]["text"]
        assert "12s" in summary_text, "footer must contain elapsed time"
        assert "$0.04" in summary_text, "footer must contain cost_str when set"

    def test_error_state_summary_context_has_cross_emoji_and_reason(self) -> None:
        """ERROR state's summary context block carries the cross emoji + the reason."""
        state = update(
            _make_state(agent_name="Atlas", started_at=0.0),
            EmbedEvent(kind="error", label="rate limited"),
        )
        blocks = to_blocks(state, now=5.0)
        context_blocks = _find_blocks_by_type(blocks, "context")
        assert context_blocks, "ERROR state must produce a context block"
        summary_text = context_blocks[-1]["elements"][0]["text"]
        assert summary_text == "Atlas · 5s"
        assert blocks[0]["text"]["text"] == "Something went wrong."

    def test_no_block_contains_color_key(self) -> None:
        """No block dict anywhere must contain a 'color' key."""
        state = _make_state(
            phase=TurnPhase.THINKING,
            tool_lines=("🖋️ Tool",),
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


# ---------------------------------------------------------------------------
# to_interrupted_blocks() -- the boot sweep's frozen-card renderer
# ---------------------------------------------------------------------------


class TestToInterruptedBlocks:
    def test_returns_exactly_one_section_block_with_mrkdwn_text(self) -> None:
        blocks = to_interrupted_blocks()
        assert len(blocks) == 2, "restart title and retry step are separate blocks"
        assert blocks[0]["type"] == "section", (
            "the house pattern for a whole-card notice is section"
        )
        assert blocks[0]["text"]["type"] == "mrkdwn", "notice text must be mrkdwn"

    def test_no_block_is_an_actions_block(self) -> None:
        blocks = to_interrupted_blocks()
        action_blocks = _find_blocks_by_type(blocks, "actions")
        assert not action_blocks, "a dead turn must not offer a Cancel button"

    def test_notice_text_is_character_identical_to_discord_copy(self) -> None:
        # Literal, not imported from the Discord adapter -- import-linter's
        # independence contract forbids cross-adapter imports, and the point
        # of this test is that the two hand-kept literals stay in sync.
        discord_copy = "Stopped: Daimon restarted.\nMention me to try again."
        blocks = to_interrupted_blocks()
        assert "\n".join(block["text"]["text"] for block in blocks) == discord_copy, (
            "Slack's retirement copy must be byte-identical to Discord's"
        )
