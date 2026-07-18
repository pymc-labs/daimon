"""Tests for DiscordTurnLifecycle — embed state machine, debounce, clean replace.

Uses plain async recorder functions. No AsyncMock, no MagicMock,
no FakeMessage for lifecycle send/edit mocks. The edit callable receives the
message reference as its first positional argument.
"""

from __future__ import annotations

import dataclasses
import time
import types
from datetime import UTC, datetime
from typing import Any

import discord
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.theme import COLOR_RED
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL_REF = object()  # opaque message reference


def _make_lifecycle(
    agent_name: str = "test-agent",
    cancel_view: discord.ui.View | None = None,
    model_id: str = "claude-sonnet-4-6",
) -> tuple[DiscordTurnLifecycle, list[dict[str, Any]], list[tuple[Any, dict[str, Any]]]]:
    """Create lifecycle with recorder callables.

    Returns (lifecycle, sends, edits) where:
    - sends: list of kwargs dicts passed to send
    - edits: list of (ref, kwargs) tuples passed to edit
    """
    sends: list[dict[str, Any]] = []
    edits: list[tuple[Any, dict[str, Any]]] = []

    async def fake_send(**kwargs: Any) -> object:
        sends.append(kwargs)
        return _SENTINEL_REF

    async def fake_edit(ref: Any, **kwargs: Any) -> None:
        edits.append((ref, kwargs))

    lc = DiscordTurnLifecycle(
        send=fake_send,
        edit=fake_edit,
        agent_name=agent_name,
        model_id=model_id,
        cancel_view=cancel_view,
    )
    return lc, sends, edits


def _thinking_event() -> Any:
    """MA session SSE event: agent.thinking."""
    return types.SimpleNamespace(type="agent.thinking")


def _tool_use_event(name: str = "Bash") -> Any:
    """MA session SSE event: agent.tool_use."""
    return types.SimpleNamespace(type="agent.tool_use", name=name)


def _message_event(text: str = "I'll look that up") -> Any:
    """MA session SSE event: agent.message with content blocks."""
    content = [types.SimpleNamespace(text=text)]
    return types.SimpleNamespace(type="agent.message", content=content)


def _make_success_state(text: str = "Hello response") -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=text)])


# ---------------------------------------------------------------------------
# SPEC-R1: First SSE event posts embed immediately
# ---------------------------------------------------------------------------


class TestFirstEventSendsEmbed:
    async def test_first_sse_event_posts_embed(self) -> None:
        """First SSE event causes an embed to be posted to the thread."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())

        assert len(sends) == 1, "first event should post embed"
        assert "embeds" in sends[0], "post should include embeds kwarg"

    async def test_second_event_within_debounce_does_not_edit(self) -> None:
        """Subsequent events within the 10s debounce window do NOT trigger edits."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        await lc.on_sse_event(_tool_use_event("read_file"))

        assert len(sends) == 1, "only one send — no additional posts"
        assert len(edits) == 0, "no edits within debounce window"


class TestPostInitial:
    async def test_post_initial_sends_thinking_embed_immediately(self) -> None:
        """post_initial posts the thinking embed without waiting for SSE events,
        giving instant feedback while session setup (a potentially minutes-long
        sessions.create) runs."""
        lc, sends, edits = _make_lifecycle()

        await lc.post_initial()

        assert len(sends) == 1, "post_initial should post the embed immediately"
        embed: discord.Embed = sends[0]["embeds"][0]
        assert embed.title is not None and "thinking" in embed.title, (
            "initial embed should render the thinking phase"
        )
        assert len(edits) == 0, "no edits before any SSE event"

    async def test_sse_event_after_post_initial_edits_instead_of_resending(self) -> None:
        """The initial embed message is adopted as the lifecycle's message ref —
        SSE flushes edit it in place rather than posting a second embed."""
        lc, sends, edits = _make_lifecycle()

        await lc.post_initial()
        # Simulate debounce elapsed by backdating last flush
        lc._last_flush = time.monotonic() - 11.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce is the established idiom in TestDebounce
        await lc.on_sse_event(_tool_use_event("Bash"))

        assert len(sends) == 1, "initial embed should be adopted, not re-posted"
        assert len(edits) == 1, "SSE flush should edit the initial embed in place"
        assert edits[0][0] is _SENTINEL_REF, "edit should target the initial embed's message ref"


# ---------------------------------------------------------------------------
# SPEC-R5: Debounce
# ---------------------------------------------------------------------------


class TestDebounce:
    async def test_event_after_debounce_window_triggers_edit(self) -> None:
        """Event after 10s debounce window triggers an edit."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        # Simulate debounce elapsed by backdating last flush
        lc._last_flush = time.monotonic() - 11.0

        await lc.on_sse_event(_tool_use_event("Bash"))

        assert len(edits) == 1, "edit should fire after debounce elapsed"
        assert edits[0][0] is _SENTINEL_REF, "edit should use stored message ref"

    async def test_terminal_flushes_immediately_regardless_of_debounce(self) -> None:
        """Terminal success bypasses debounce and flushes immediately."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        # Debounce NOT elapsed — but terminal should flush anyway
        state = _make_success_state("done")
        await lc.on_terminal_success(state)

        # After terminal_success: flush_terminal calls edit (done embed),
        # then clean replace calls edit again with content=
        assert len(edits) >= 1, "terminal should trigger at least one edit"


# ---------------------------------------------------------------------------
# SPEC-R6: Clean replace on terminal success
# ---------------------------------------------------------------------------


class TestCleanReplace:
    async def test_terminal_success_replaces_embed_with_text(self) -> None:
        """Terminal success replaces embed with plain text (clean replace)."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        state = _make_success_state("Hello response")
        await lc.on_terminal_success(state)

        # Last edit should be clean replace: content=text, embed=None, view=None
        replace_edit = edits[-1]
        assert replace_edit[0] is _SENTINEL_REF, "edit should use stored message ref"
        assert replace_edit[1].get("content") == "Hello response"
        assert replace_edit[1].get("embed") is None
        assert replace_edit[1].get("view") is None

    async def test_long_response_splits_into_overflow(self) -> None:
        """Long response: first chunk replaces embed, overflow chunks are new sends."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        initial_sends = len(sends)

        # 4000 chars will split into multiple chunks (limit is 1900)
        long_text = "x" * 4000
        state = TurnState(content=[TextBlock(kind="text", text=long_text)])
        await lc.on_terminal_success(state)

        # The clean replace edit must have a content kwarg
        replace_edit = edits[-1]
        assert replace_edit[1].get("embed") is None, "clean replace: no embed"

        # Overflow chunks posted as new sends (beyond the initial embed send)
        overflow_sends = len(sends) - initial_sends
        assert overflow_sends >= 1, "overflow chunks should be posted as new sends"


# ---------------------------------------------------------------------------
# SPEC-R7: Error embed on terminal failure
# ---------------------------------------------------------------------------


class TestErrorEmbed:
    async def test_terminal_failure_shows_error_embed(self) -> None:
        """Terminal failure shows a red error embed that stays visible (not clean replaced)."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        state = TurnState()
        await lc.on_terminal_failure(state, Exception("timeout"))

        # Failure flushes terminal as embed (edit with embed=...), no content= key
        last_edit = edits[-1]
        assert last_edit[0] is _SENTINEL_REF, "edit should use stored message ref"
        assert "embeds" in last_edit[1], "error embed should be present"
        assert last_edit[1].get("content") is None or "content" not in last_edit[1], (
            "error path should NOT clean replace (embed stays visible)"
        )

    async def test_error_embed_has_red_color(self) -> None:
        """Error embed color is 0xED4245 (red)."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        state = TurnState()
        await lc.on_terminal_failure(state, Exception("boom"))

        last_edit = edits[-1]
        embeds = last_edit[1].get("embeds")
        assert embeds, "error embed must be present"
        embed = embeds[0]
        assert embed.colour.value == COLOR_RED, (  # type: ignore[union-attr]
            f"error embed color must be {COLOR_RED:#x}"
        )


# ---------------------------------------------------------------------------
# on_render is a no-op
# ---------------------------------------------------------------------------


class TestOnRenderNoop:
    async def test_on_render_is_noop_when_embed_posted(self) -> None:
        """on_render leaves the embed alone — embed is driven by SSE events, not
        render ticks — and posts nothing for unsealed trailing text."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        initial_sends = len(sends)
        initial_edits = len(edits)

        state = _make_success_state("some content")
        await lc.on_render(state)

        assert len(sends) == initial_sends, "on_render must not trigger additional sends"
        assert len(edits) == initial_edits, "on_render must not trigger edits"


# ---------------------------------------------------------------------------
# Sealed-response persistence: answers composed before a trailing tool call
# (e.g. the memory-PR routine) must post permanently instead of being
# swallowed by the final-response extraction.
# ---------------------------------------------------------------------------


def _sealed_state(answer: str, *, trailing: str = "") -> TurnState:
    content: list[Any] = [
        TextBlock(kind="text", text=answer),
        ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
    ]
    if trailing:
        content.append(TextBlock(kind="text", text=trailing))
    return TurnState(content=content)


class TestSealedResponsePersistence:
    async def test_on_render_posts_sealed_answer_once(self) -> None:
        """A >=500-char text block sealed by a tool use posts as a permanent
        message on the next render tick — and only once across ticks."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        initial_sends = len(sends)

        answer = "The full diagnosis is: " + "x" * 600
        state = _sealed_state(answer)
        await lc.on_render(state)
        await lc.on_render(state)

        content_sends = [s for s in sends[initial_sends:] if "content" in s]
        assert len(content_sends) == 1, "sealed answer should post exactly once across ticks"
        assert content_sends[0]["content"] == answer, "the sealed text posts verbatim"

    async def test_on_render_keeps_short_narration_suppressed(self) -> None:
        """Sealed text under the threshold is narration and never posts."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        initial_sends = len(sends)

        await lc.on_render(_sealed_state("Let me check the ArviZ summary."))

        assert len(sends) == initial_sends, "short pre-tool narration must not post"

    async def test_terminal_success_posts_unflushed_sealed_answer_before_final(self) -> None:
        """A sealed answer the render loop never flushed still posts at terminal,
        and the final recap posts as today."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        initial_sends = len(sends)

        answer = "Here is the verified diagnosis. " + "y" * 600
        state = _sealed_state(answer, trailing="I've delivered the full diagnosis above.")
        await lc.on_terminal_success(state)

        content_sends = [s["content"] for s in sends[initial_sends:] if "content" in s]
        assert content_sends == [answer], "unflushed sealed answer posts at terminal"
        replace_edit = edits[-1]
        assert replace_edit[1].get("content") == "I've delivered the full diagnosis above.", (
            "final recap still replaces the embed"
        )

    async def test_terminal_success_does_not_repost_already_flushed_answer(self) -> None:
        """A sealed answer posted by on_render is not re-posted at terminal."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        initial_sends = len(sends)

        answer = "z" * 800
        state = _sealed_state(answer, trailing="Recap.")
        await lc.on_render(state)
        await lc.on_terminal_success(state)

        content_sends = [s["content"] for s in sends[initial_sends:] if "content" in s]
        assert content_sends == [answer], "sealed answer posts exactly once end-to-end"

    async def test_terminal_success_with_sealed_answer_and_no_final_text_keeps_done_embed(
        self,
    ) -> None:
        """Tool-only ending after a flushed sealed answer keeps the done embed
        (no 'Turn cancelled' replace)."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        state = _sealed_state("w" * 700)
        await lc.on_terminal_success(state)

        assert all(e[1].get("content") != "Turn cancelled." for e in edits), (
            "a turn that posted a sealed answer is not a cancellation"
        )


# ---------------------------------------------------------------------------
# Cancel view wiring
# ---------------------------------------------------------------------------


class TestCancelViewWiring:
    async def test_first_send_includes_cancel_view(self) -> None:
        """When cancel_view is set, first embed send passes view= kwarg."""
        fake_view = discord.ui.View()
        lc, sends, edits = _make_lifecycle(cancel_view=fake_view)
        await lc.on_sse_event(_thinking_event())
        assert sends[0].get("view") is fake_view, "first send must include cancel_view"

    async def test_debounced_edit_includes_cancel_view(self) -> None:
        """Debounced edit passes view= kwarg."""
        fake_view = discord.ui.View()
        lc, sends, edits = _make_lifecycle(cancel_view=fake_view)
        await lc.on_sse_event(_thinking_event())
        lc._last_flush = time.monotonic() - 11.0
        await lc.on_sse_event(_tool_use_event("Bash"))
        assert edits[0][1].get("view") is fake_view, "debounced edit must include cancel_view"

    async def test_terminal_success_removes_cancel_view(self) -> None:
        """Terminal success clean-replace passes view=None."""
        fake_view = discord.ui.View()
        lc, sends, edits = _make_lifecycle(cancel_view=fake_view)
        await lc.on_sse_event(_thinking_event())
        state = _make_success_state("Hello")
        await lc.on_terminal_success(state)
        # The clean-replace edit (last one) must pass view=None
        replace_edit = edits[-1]
        assert replace_edit[1].get("view") is None, "clean-replace must remove cancel_view"

    async def test_terminal_failure_removes_cancel_view(self) -> None:
        """Terminal failure flush passes view=None."""
        fake_view = discord.ui.View()
        lc, sends, edits = _make_lifecycle(cancel_view=fake_view)
        await lc.on_sse_event(_thinking_event())
        state = TurnState()
        await lc.on_terminal_failure(state, Exception("boom"))
        # _flush_terminal edit must pass view=None
        last_edit = edits[-1]
        assert last_edit[1].get("view") is None, "error flush must remove cancel_view"

    async def test_no_cancel_view_sends_without_view_kwarg(self) -> None:
        """When cancel_view is None (default), send passes view=None."""
        lc, sends, edits = _make_lifecycle()  # no cancel_view
        await lc.on_sse_event(_thinking_event())
        # view kwarg should be None (or absent) -- no view attached
        assert sends[0].get("view") is None, "no cancel_view means view=None on send"

    async def test_terminal_success_with_empty_content_sends_turn_cancelled(self) -> None:
        """Cancelled turn with no content replaces embed with 'Turn cancelled.'."""
        fake_view = discord.ui.View()
        lc, sends, edits = _make_lifecycle(cancel_view=fake_view)
        await lc.on_sse_event(_thinking_event())
        # Empty state -- no TextBlock content (simulates cancel before any output)
        state = TurnState()
        await lc.on_terminal_success(state)
        # The last edit should be the "Turn cancelled." clean-replace
        cancel_edit = edits[-1]
        assert cancel_edit[1].get("content") == "Turn cancelled.", (
            "empty-content terminal success must show 'Turn cancelled.'"
        )
        assert cancel_edit[1].get("embed") is None, (
            "empty-content terminal success must remove embed"
        )
        assert cancel_edit[1].get("view") is None, (
            "empty-content terminal success must remove cancel view"
        )


# ---------------------------------------------------------------------------
# Thinking is a contentless ping: phase/title only, no trail line
# ---------------------------------------------------------------------------


class TestThinkingNotInTrail:
    async def test_thinking_then_tool_trail_has_tool_line_only(self) -> None:
        """A thinking ping followed by a tool call yields a trail with the tool
        line only — the thinking ping leaves no entry behind."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        lc._last_flush = time.monotonic() - 11.0
        await lc.on_sse_event(_tool_use_event("Bash"))

        embed = edits[-1][1]["embeds"][0]
        assert "Bash" in (embed.description or ""), "tool line must be present"
        assert "thinking" not in (embed.description or ""), "no thinking line in the trail"

    async def test_thinking_then_message_surfaces_preview_embed(self) -> None:
        """The real intermediate content arrives via agent.message and is surfaced
        in the bottom preview embed — not the activity trail."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        lc._last_flush = time.monotonic() - 11.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce is the established idiom in TestDebounce
        await lc.on_sse_event(_message_event("Let me check the workspace config"))

        embeds = edits[-1][1]["embeds"]
        assert len(embeds) == 2, "activity embed on top, preview embed below"
        assert "Let me check the workspace config" not in (embeds[0].description or ""), (
            "message text must not enter the activity trail"
        )
        assert "Let me check the workspace config" in (embeds[1].description or ""), (
            "agent.message text must be surfaced in the preview embed"
        )


# ---------------------------------------------------------------------------
# Filtered extraction (extract_final_response integration)
# ---------------------------------------------------------------------------


class TestFilteredExtraction:
    async def test_multi_tool_turn_shows_only_final_response(self) -> None:
        """Multi-tool turn: intermediate narration filtered, only final text shown."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        state = TurnState(
            content=[
                TextBlock(kind="text", text="I'll look that up. "),
                ToolUseBlock(
                    kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}
                ),
                TextBlock(kind="text", text="Here is the answer."),
            ]
        )
        await lc.on_terminal_success(state)

        # Clean replace should contain only "Here is the answer."
        replace_edit = edits[-1]
        assert replace_edit[1].get("content") == "Here is the answer."
        assert replace_edit[1].get("embed") is None

    async def test_no_tool_turn_shows_all_text(self) -> None:
        """No-tool turn: all text is final, shown in clean replace."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        state = TurnState(
            content=[
                TextBlock(kind="text", text="Hello world"),
            ]
        )
        await lc.on_terminal_success(state)

        replace_edit = edits[-1]
        assert replace_edit[1].get("content") == "Hello world"


# ---------------------------------------------------------------------------
# Zero-message vs cancelled disambiguation
# ---------------------------------------------------------------------------


class TestZeroMessageBehavior:
    async def test_zero_message_with_tools_leaves_done_embed_visible(self) -> None:
        """tools ran but no final text -> done embed stays, no clean-replace."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        # Tools ran but no TextBlock after the last tool
        state = TurnState(
            content=[
                TextBlock(kind="text", text="I'll run that."),
                ToolUseBlock(
                    kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}
                ),
            ]
        )
        await lc.on_terminal_success(state)

        # Only the terminal flush edit (done embed), no clean-replace edit
        # The flush_terminal edit sets embed= (done embed). No subsequent content= edit.
        assert len(edits) == 1, "only terminal flush edit, no clean-replace"
        assert "embeds" in edits[0][1], "terminal flush should have embed"

    async def test_truly_cancelled_turn_shows_turn_cancelled(self) -> None:
        """Empty content (no blocks at all) still shows 'Turn cancelled.'."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        state = TurnState()  # completely empty content
        await lc.on_terminal_success(state)

        cancel_edit = edits[-1]
        assert cancel_edit[1].get("content") == "Turn cancelled."
        assert cancel_edit[1].get("embed") is None


# ---------------------------------------------------------------------------
# agent.message SSE event mapping
# ---------------------------------------------------------------------------


class TestMessageEventMapping:
    async def test_message_event_produces_preview_embed(self) -> None:
        """agent.message SSE events surface their text in the bottom preview embed."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_message_event(text="I'll look that up for you and check"))

        assert len(sends) == 1
        embeds = sends[0].get("embeds")
        assert embeds is not None and len(embeds) == 2
        assert "I'll look that up" in embeds[1].description, (
            "message text belongs to the preview embed"
        )

    async def test_thinking_event_shows_phase_in_title_not_trail(self) -> None:
        """agent.thinking posts the embed and shows the thinking phase in the title,
        but adds no trail line — MA emits no thinking text to surface."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        assert len(sends) == 1
        embeds: list[discord.Embed] | None = sends[0].get("embeds")
        assert embeds is not None
        embed = embeds[0]
        assert "thinking" in (embed.title or ""), "title should show the thinking phase"
        assert "thinking" not in (embed.description or ""), (
            "trail must not carry a contentless thinking line"
        )

    async def test_message_event_truncates_long_text(self) -> None:
        """Long agent.message text is capped at 250 chars in the preview embed."""
        lc, sends, edits = _make_lifecycle()
        long_text = "A" * 400
        await lc.on_sse_event(_message_event(text=long_text))

        embeds = sends[0].get("embeds")
        assert embeds is not None and len(embeds) == 2
        preview = embeds[1].description
        assert len(preview) < 400, "preview must be truncated, not the full text"
        assert "…" in preview, "truncated text should end with ellipsis"


# ---------------------------------------------------------------------------
# Turn-summary footer: usage + priced cost (matches the billing ledger)
# ---------------------------------------------------------------------------


def _span_usage_event(
    *,
    event_id: str,
    input_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    output_tokens: int,
) -> BetaManagedAgentsSpanModelRequestEndEvent:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        type="span.model_request_end",
        model_request_start_id="start_" + event_id,
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            output_tokens=output_tokens,
            speed="standard",
        ),
        processed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _terminal_embed(edits: list[tuple[Any, dict[str, Any]]]) -> discord.Embed:
    """The discord.Embed flushed at the terminal hook (first edit carrying embeds)."""
    for _ref, kwargs in edits:
        embeds = kwargs.get("embeds")
        if embeds:
            return embeds[0]
    raise AssertionError("no terminal embed was flushed")


class TestTurnSummaryFooter:
    async def test_priced_model_sets_cost_str(self) -> None:
        lc, _sends, edits = _make_lifecycle(model_id="claude-sonnet-4-6")
        await lc.on_sse_event(_thinking_event())
        state = apply(
            TurnState(),
            _span_usage_event(
                event_id="u1",
                input_tokens=1000,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                output_tokens=200,
            ),
        )
        state = dataclasses.replace(state, content=[TextBlock(kind="text", text="hi")])
        await lc.on_terminal_success(state)
        footer = _terminal_embed(edits).footer.text
        assert footer is not None and "$" in footer, "priced model footer carries a cost segment"

    async def test_unpriced_model_omits_cost(self) -> None:
        lc, _sends, edits = _make_lifecycle(model_id="unknown-model")
        await lc.on_sse_event(_thinking_event())
        state = apply(
            TurnState(),
            _span_usage_event(
                event_id="u1",
                input_tokens=1000,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                output_tokens=200,
            ),
        )
        await lc.on_terminal_failure(state, Exception("boom"))
        footer = _terminal_embed(edits).footer.text
        assert footer is not None, "footer renders even for unpriced model"
        assert "$" not in footer, "unpriced model footer omits the cost segment"
        assert "1k in / 200 out" in footer, "merged-in token count still shown"

    async def test_merged_input_count_in_footer(self) -> None:
        lc, _sends, edits = _make_lifecycle(model_id="claude-sonnet-4-6")
        await lc.on_sse_event(_thinking_event())
        state = apply(
            TurnState(),
            _span_usage_event(
                event_id="u1",
                input_tokens=1000,
                cache_creation_input_tokens=500,
                cache_read_input_tokens=2000,
                output_tokens=300,
            ),
        )
        await lc.on_terminal_failure(state, Exception("boom"))
        footer = _terminal_embed(edits).footer.text
        # merged_in = 1000 + 500 + 2000 = 3500 -> "3.5k"; out = 300
        assert footer is not None and "3.5k in / 300 out" in footer, (
            "displayed input is the merged input+cache_creation+cache_read count"
        )

    async def test_footer_cost_equals_billing_ledger_with_cache_reads(self) -> None:
        # The whole point: footer cost == cost_of for the same 4 cache-split ints.
        lc, _sends, edits = _make_lifecycle(model_id="claude-sonnet-4-6")
        await lc.on_sse_event(_thinking_event())
        state = apply(
            TurnState(),
            _span_usage_event(
                event_id="u1",
                input_tokens=1000,
                cache_creation_input_tokens=500,
                cache_read_input_tokens=2000,
                output_tokens=300,
            ),
        )
        await lc.on_terminal_success(state)

        ledger_cost = cost_of(
            BetaManagedAgentsSpanModelUsage(
                input_tokens=1000,
                cache_creation_input_tokens=500,
                cache_read_input_tokens=2000,
                output_tokens=300,
                speed="standard",
            ),
            MODEL_PRICING["claude-sonnet-4-6"],
        )
        expected = format_cost(ledger_cost)
        footer = _terminal_embed(edits).footer.text
        assert footer is not None and expected is not None
        assert expected in footer, (
            f"footer cost must equal the billing-ledger cost {expected} to the cent"
        )
