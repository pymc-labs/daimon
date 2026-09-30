"""Tests for DiscordTurnLifecycle — embed state machine, debounce, clean replace.

Uses plain async recorder functions. No AsyncMock, no MagicMock,
no FakeMessage for lifecycle send/edit mocks. The edit callable receives the
message reference as its first positional argument.
"""

from __future__ import annotations

import dataclasses
import time
import types
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NoReturn

import daimon.adapters.discord.lifecycle as lifecycle_module
import discord
import pytest
import structlog
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.theme import COLOR_RED
from daimon.core.errors import TurnError
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.stores import tenant_ledger
from daimon.core.stores.tenants import set_funding_mode
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import McpServerFailure, TextBlock, ToolUseBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL_REF = object()  # opaque message reference


def _make_lifecycle(
    agent_name: str = "test-agent",
    cancel_view: discord.ui.View | None = None,
    model_id: str = "claude-sonnet-4-6",
    notify_on_completion: bool = False,
    render_tables: bool = False,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    tenant_id: uuid.UUID | None = None,
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
        notify_on_completion=notify_on_completion,
        requester_id=123,
        render_tables=render_tables,
        edit=fake_edit,
        agent_name=agent_name,
        model_id=model_id,
        cancel_view=cancel_view,
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
    )
    return lc, sends, edits


def _thinking_event() -> Any:
    """MA session SSE event: agent.thinking."""
    return types.SimpleNamespace(type="agent.thinking")


def _running_tool_turn(name: str = "bash") -> TurnState:
    """Turn state with one tool call still waiting on its result."""
    call = ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name=name, input={})
    return TurnState(content=[call])


def _message_event(text: str = "I'll look that up") -> Any:
    """MA session SSE event: agent.message with content blocks."""
    content = [types.SimpleNamespace(text=text)]
    return types.SimpleNamespace(type="agent.message", content=content)


def _make_success_state(text: str = "Hello response") -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=text)])


# ---------------------------------------------------------------------------
# D-11: on_sse_event is a cheap local tap; on_render is the delivery path
# ---------------------------------------------------------------------------


class TestFirstEventSendsEmbed:
    async def test_on_sse_event_alone_produces_no_io(self) -> None:
        """on_sse_event is a cheap local tap (D-11): folding an SSE event into
        embed state performs no network I/O by itself. The embed post is
        delivered by the render tick, not the event."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())

        assert sends == [], "on_sse_event alone must not send"
        assert edits == [], "on_sse_event alone must not edit"

    async def test_on_render_posts_embed_folded_by_sse_event(self) -> None:
        """on_render delivers the embed state on_sse_event folded."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())

        assert len(sends) == 1, "render tick should post embed"
        assert "embeds" in sends[0], "post should include embeds kwarg"

    async def test_render_tick_within_debounce_does_not_edit(self) -> None:
        """A render tick within the 10s debounce window after the first post
        does not trigger a repeat edit."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())  # first post
        await lc.on_render(_running_tool_turn("read"))  # same debounce window

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
        assert (embed.description or "").startswith("**Thinking**"), (
            "initial embed should lead with the Thinking headline"
        )
        assert len(edits) == 0, "no edits before any SSE event"

    async def test_render_after_sse_event_edits_instead_of_resending(self) -> None:
        """The initial embed message is adopted as the lifecycle's message ref —
        the render tick edits it in place rather than posting a second embed."""
        lc, sends, edits = _make_lifecycle()

        await lc.post_initial()
        # Simulate debounce elapsed by backdating last flush
        lc._last_flush = time.monotonic() - 11.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce is the established idiom in TestDebounce
        await lc.on_render(_running_tool_turn())

        assert len(sends) == 1, "initial embed should be adopted, not re-posted"
        assert len(edits) == 1, "render tick should edit the initial embed in place"
        assert edits[0][0] is _SENTINEL_REF, "edit should target the initial embed's message ref"


# ---------------------------------------------------------------------------
# SPEC-R5: Debounce
# ---------------------------------------------------------------------------


class TestDebounce:
    async def test_render_after_debounce_window_triggers_edit(self) -> None:
        """A render tick after the 10s debounce window triggers an edit."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        # Simulate debounce elapsed by backdating last flush
        lc._last_flush = time.monotonic() - 11.0

        await lc.on_render(_running_tool_turn())

        assert len(edits) == 1, "edit should fire after debounce elapsed"
        assert edits[0][0] is _SENTINEL_REF, "edit should use stored message ref"

    async def test_terminal_flushes_immediately_with_no_render_tick(self) -> None:
        """Terminal success bypasses on_render and the debounce entirely --
        _flush_terminal is called directly by the terminal hook."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        # No on_render call anywhere in this test -- terminal should still flush.
        state = _make_success_state("done")
        await lc.on_terminal_success(state)

        # message_ref is unset (on_sse_event alone performs no I/O), so
        # _flush_terminal posts the done embed via send; the clean-replace
        # step then edits that same message with the final text.
        assert len(sends) == 1, "flush_terminal posts since no message_ref exists yet"
        assert len(edits) == 1, "clean replace edits the just-posted message"


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
        await lc.on_render(TurnState())  # establishes message_ref before terminal
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

    async def test_final_answer_with_everyone_disables_all_mention_channels(self) -> None:
        """T-22-01: a final answer containing ``@everyone`` (reachable via prompt
        injection through tool output) must not ping the guild. The clean-replace
        edit must carry an AllowedMentions with everyone/roles/users all disabled."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        state = _make_success_state("@everyone check this out")
        await lc.on_terminal_success(state)

        replace_edit = edits[-1]
        mentions = replace_edit[1].get("allowed_mentions")
        assert mentions is not None, "clean-replace edit must carry allowed_mentions"
        assert not mentions.everyone, "everyone mentions must be disabled"
        assert not mentions.roles, "role mentions must be disabled"
        assert not mentions.users, "user mentions must be disabled"

    async def test_overflow_chunk_disables_all_mention_channels(self) -> None:
        """The overflow-chunk send (posted after the first chunk) must carry the
        same none-everything AllowedMentions as the clean-replace edit."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())  # establishes message_ref before terminal
        initial_sends = len(sends)

        long_text = "@everyone " + "x" * 4000
        state = TurnState(content=[TextBlock(kind="text", text=long_text)])
        await lc.on_terminal_success(state)

        overflow = sends[initial_sends:]
        assert overflow, "overflow chunks should be posted as new sends"
        mentions = overflow[0].get("allowed_mentions")
        assert mentions is not None, "overflow send must carry allowed_mentions"
        assert not mentions.everyone, "everyone mentions must be disabled"
        assert not mentions.roles, "role mentions must be disabled"
        assert not mentions.users, "user mentions must be disabled"


# ---------------------------------------------------------------------------
# SPEC-R7: Error embed on terminal failure
# ---------------------------------------------------------------------------


_NOTICE_REASONS = [
    TerminationReason.CONNECTION_LOST,
    TerminationReason.UPSTREAM,
    TerminationReason.INTERRUPTED,
    TerminationReason.INTERRUPT_TIMEOUT,
    TerminationReason.REQUIRES_ACTION,
    TerminationReason.CEILING,
    TerminationReason.MCP_DEGRADED_EMPTY,
]


@pytest.mark.parametrize("reason", _NOTICE_REASONS, ids=str)
async def test_terminal_failure_card_carries_the_termination_notice(
    reason: TerminationReason,
) -> None:
    """The red card explains the reason: headline in the footer, the rest in the body."""
    lc, _, edits = _make_lifecycle()
    await lc.on_render(TurnState())
    state = TurnState(
        termination=reason,
        content=[
            ToolUseBlock(
                kind="tool_use", id="tu_1", type="agent.tool_use", name="fit_model", input={}
            )
        ],
    )

    await lc.on_terminal_failure(state, Exception("x" * 300))

    embed = edits[-1][1]["embeds"][0]
    notice = render_termination_notice(reason, state=state)
    assert notice is not None
    assert embed.footer.text.startswith(f"❌ {notice.headline} · ")
    assert notice.cause in embed.description
    assert notice.next_step in embed.description, "the next step is not truncated away"
    assert "`fit_model`" in embed.description, "work in flight is named"
    assert "`rid: " in embed.description
    assert "xxx" not in embed.description + embed.footer.text, "raw error stays in the logs"


async def test_terminal_failure_notice_fits_discord_limits_with_many_long_names() -> None:
    """45 failed servers and 45 running tools, every name 100 characters: the
    card still fits an embed description (4,096) and footer (2,048)."""
    lc, _, edits = _make_lifecycle()
    await lc.on_render(TurnState())
    state = TurnState(
        termination=TerminationReason.MCP_DEGRADED_EMPTY,
        mcp_failures=tuple(
            McpServerFailure(
                server_name=f"{i:02d}" + "s" * 98,
                error_type="mcp_connection_failed_error",
                message="down",
                retry_status="exhausted",
            )
            for i in range(45)
        ),
        content=[
            ToolUseBlock(
                kind="tool_use",
                id=f"tu_{i}",
                type="agent.tool_use",
                name=f"{i:02d}" + "t" * 98,
                input={},
            )
            for i in range(45)
        ],
    )

    await lc.on_terminal_failure(state, Exception("x"))

    embed = edits[-1][1]["embeds"][0]
    assert len(embed.description) <= 4096
    assert len(embed.footer.text) <= 2048
    assert "and 42 more" in embed.description and "and 40 more" in embed.description
    assert "`rid: " in embed.description


async def test_a_notice_that_fails_to_build_still_turns_the_card_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("renderer broke")

    monkeypatch.setattr(lifecycle_module, "render_termination_notice", _broken)
    lc, _, edits = _make_lifecycle()
    await lc.on_render(TurnState())

    await lc.on_terminal_failure(TurnState(), Exception("upstream timeout"))

    embed = edits[-1][1]["embeds"][0]
    assert embed.colour.value == COLOR_RED
    assert embed.footer.text.startswith("❌ upstream timeout · "), "falls back to the raw label"
    assert not embed.description


async def test_the_card_reuses_the_rid_bound_for_the_turn() -> None:
    lc, _, edits = _make_lifecycle()
    await lc.on_render(TurnState())

    with structlog.contextvars.bound_contextvars(rid="01BOUNDRID"):
        await lc.on_terminal_failure(TurnState(), Exception("x"))

    assert "`rid: 01BOUNDRID`" in edits[-1][1]["embeds"][0].description


async def test_terminal_failure_without_a_reason_on_the_state_maps_the_error() -> None:
    lc, _, edits = _make_lifecycle()
    await lc.on_render(TurnState())

    await lc.on_terminal_failure(TurnState(), TurnError(kind="connection_lost"))

    assert edits[-1][1]["embeds"][0].footer.text.startswith("❌ Connection lost · ")


class TestErrorEmbed:
    async def test_terminal_failure_shows_error_embed(self) -> None:
        """Terminal failure shows a red error embed that stays visible (not clean replaced)."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
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
        await lc.on_render(TurnState())
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
# T-19-07-B: on_render must not swallow adapter failures -- the driver's
# per-tick render error policy (plan 19-06) is what handles them.
# ---------------------------------------------------------------------------


class TestRenderPropagatesFailures:
    async def test_raising_edit_propagates_out_of_on_render(self) -> None:
        """A rate-limited/failing Discord edit surfaces out of on_render --
        the adapter does not swallow it."""

        async def _raising_edit(ref: Any, **kwargs: Any) -> None:
            raise RuntimeError("rate limited")

        sends: list[dict[str, Any]] = []

        async def _send(**kwargs: Any) -> object:
            sends.append(kwargs)
            return _SENTINEL_REF

        lc = DiscordTurnLifecycle(
            send=_send,
            edit=_raising_edit,
            agent_name="test-agent",
            model_id="claude-sonnet-4-6",
        )

        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())  # first post -- no edit yet, succeeds
        lc._last_flush = time.monotonic() - 11.0

        with pytest.raises(RuntimeError, match="rate limited"):
            await lc.on_render(_running_tool_turn())

    async def test_on_sse_event_never_raises_for_the_same_scenario(self) -> None:
        """The cheap local tap performs no I/O, so a failing edit callable
        never reaches it -- only the render tick can hit that failure."""

        async def _raising_edit(ref: Any, **kwargs: Any) -> None:
            raise RuntimeError("rate limited")

        async def _send(**kwargs: Any) -> object:
            return _SENTINEL_REF

        lc = DiscordTurnLifecycle(
            send=_send,
            edit=_raising_edit,
            agent_name="test-agent",
            model_id="claude-sonnet-4-6",
        )

        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        lc._last_flush = time.monotonic() - 11.0
        # Does not raise even though the next render tick would hit the
        # raising edit -- on_sse_event performs no I/O at all.
        await lc.on_sse_event(_message_event("Checking the logs"))
        assert lc._state.text_preview == "Checking the logs", (  # pyright: ignore[reportPrivateUsage]
            "the tap still folded the event into the card"
        )


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

    async def test_sealed_answer_disables_all_mention_channels(self) -> None:
        """T-22-01: the sealed pre-tool answer send (reachable via prompt
        injection through tool output) must disable everyone/role/user mentions."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        initial_sends = len(sends)

        answer = "@everyone the full diagnosis is: " + "x" * 600
        state = _sealed_state(answer)
        await lc.on_render(state)

        content_sends = [s for s in sends[initial_sends:] if "content" in s]
        assert len(content_sends) == 1
        mentions = content_sends[0].get("allowed_mentions")
        assert mentions is not None, "sealed answer send must carry allowed_mentions"
        assert not mentions.everyone, "everyone mentions must be disabled"
        assert not mentions.roles, "role mentions must be disabled"
        assert not mentions.users, "user mentions must be disabled"

    async def test_on_render_keeps_short_narration_suppressed(self) -> None:
        """Sealed text under the threshold is narration and never posts as a
        standalone message (on_render's own embed flush is unrelated)."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        await lc.on_render(_sealed_state("Let me check the ArviZ summary."))

        content_sends = [s for s in sends if "content" in s]
        assert content_sends == [], "short pre-tool narration must not post"

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
        await lc.on_render(TurnState())

        state = _sealed_state("w" * 700)
        await lc.on_terminal_success(state)

        assert edits, "the done embed flush must still land"
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
        await lc.on_render(TurnState())
        assert sends[0].get("view") is fake_view, "first send must include cancel_view"

    async def test_debounced_edit_includes_cancel_view(self) -> None:
        """Debounced edit passes view= kwarg."""
        fake_view = discord.ui.View()
        lc, sends, edits = _make_lifecycle(cancel_view=fake_view)
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        lc._last_flush = time.monotonic() - 11.0
        await lc.on_render(_running_tool_turn())
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
        await lc.on_render(TurnState())
        state = TurnState()
        await lc.on_terminal_failure(state, Exception("boom"))
        # _flush_terminal edit must pass view=None
        last_edit = edits[-1]
        assert last_edit[1].get("view") is None, "error flush must remove cancel_view"

    async def test_no_cancel_view_sends_without_view_kwarg(self) -> None:
        """When cancel_view is None (default), send passes view=None."""
        lc, sends, edits = _make_lifecycle()  # no cancel_view
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
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
# One status embed, built from the turn state on each render
# ---------------------------------------------------------------------------


class TestStatusEmbedFromTurnState:
    async def test_render_lists_every_tool_kind_from_turn_state(self) -> None:
        """Tool lines come from the render's TurnState, so MCP calls show too,
        not only the agent.tool_use events the SSE tap sees."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_render(TurnState(content=[TextBlock(kind="text", text="Checking.")]))
        lc._last_flush = time.monotonic() - 11.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce is the established idiom in TestDebounce
        mcp_call = ToolUseBlock(
            kind="tool_use",
            id="tu_1",
            type="agent.mcp_tool_use",
            name="search_issues",
            input={"query": "private words"},
            mcp_server_name="tracker",
        )
        await lc.on_render(TurnState(content=[mcp_call]))

        embeds = edits[-1][1]["embeds"]
        assert len(embeds) == 1, "the whole status is one embed"
        description = embeds[0].description or ""
        assert description.startswith("**Working**"), "a pending call reads as working"
        assert "🔍 Search issues (tracker)" in description, "MCP calls get a readable line"
        assert "private words" not in description, "a tool line never shows its arguments"

    async def test_message_draft_shares_the_status_embed(self) -> None:
        """The latest agent.message text is quoted under the tool lines, in the
        same embed rather than a second one."""
        lc, sends, edits = _make_lifecycle()

        await lc.on_render(TurnState())
        lc._last_flush = time.monotonic() - 11.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce is the established idiom in TestDebounce
        await lc.on_sse_event(_message_event("Let me check the workspace config"))
        await lc.on_render(TurnState())

        embeds = edits[-1][1]["embeds"]
        assert len(embeds) == 1, "the draft rides the status embed"
        assert (embeds[0].description or "").endswith("> Let me check the workspace config"), (
            "agent.message text must be quoted at the bottom of the status embed"
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
        await lc.on_render(TurnState())

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
    async def test_message_event_produces_draft(self) -> None:
        """agent.message SSE events surface their text as the status embed's draft."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_message_event(text="I'll look that up for you and check"))
        await lc.on_render(TurnState())

        embeds = sends[0].get("embeds")
        assert embeds is not None and len(embeds) == 1, "one status embed"
        assert "> I'll look that up" in embeds[0].description, "message text is the draft"

    async def test_thinking_event_adds_nothing_to_the_card(self) -> None:
        """agent.thinking carries no text; the headline already says Thinking."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())

        embeds: list[discord.Embed] | None = sends[0].get("embeds")
        assert embeds is not None
        description = embeds[0].description or ""
        assert description.startswith("**Thinking**"), "headline shows the thinking state"
        assert "\n" not in description, "no tool lines and no draft for a bare thinking ping"

    async def test_message_event_truncates_long_text(self) -> None:
        """Long agent.message text is capped at 300 chars in the draft."""
        lc, sends, edits = _make_lifecycle()
        long_text = "A" * 400
        await lc.on_sse_event(_message_event(text=long_text))
        await lc.on_render(TurnState())

        embeds = sends[0].get("embeds")
        assert embeds is not None and len(embeds) == 1, "the draft rides the one status embed"
        description = embeds[0].description
        assert len(description) < 400, "draft must be truncated, not the full text"
        assert "…" in description, "truncated text should end with ellipsis"


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


@pytest.mark.asyncio
async def test_terminal_footer_shows_prepaid_balance_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    async with db_session_factory() as s, s.begin():
        await tenant_ledger.insert_entry(
            s,
            tenant_id=tenant.id,
            delta_usd=Decimal("12.50"),
            reason="test",
            idempotency_key=f"test:{tenant.id}",
        )
    lc, _sends, edits = _make_lifecycle(sessionmaker=db_session_factory, tenant_id=tenant.id)
    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())
    await lc.on_terminal_success(_make_success_state())
    assert _terminal_embed(edits).footer.text.endswith("· $12.50 left")

    async with db_session_factory() as s, s.begin():
        await set_funding_mode(s, tenant_id=tenant.id, funding_mode="operator_funded")
    lc, _sends, edits = _make_lifecycle(sessionmaker=db_session_factory, tenant_id=tenant.id)
    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())
    await lc.on_terminal_success(_make_success_state())
    assert "$12.50 left" not in _terminal_embed(edits).footer.text


class TestWasAnswered:
    async def test_was_answered_is_false_when_no_terminal_hook_called(
        self,
    ) -> None:
        """A lifecycle that never reached a terminal hook has not answered."""
        lc, _sends, _edits = _make_lifecycle()

        assert lc.was_answered is False, (
            "a lifecycle with no terminal hook called must not report an answer"
        )

    async def test_was_answered_is_true_when_terminal_success_produces_final_text(self) -> None:
        """A real text answer marks the turn as answered."""
        lc, _sends, _edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        state = TurnState(content=[TextBlock(kind="text", text="Hello response")])
        await lc.on_terminal_success(state)

        assert lc.was_answered is True, "a turn that produced final text must report an answer"

    async def test_was_answered_is_true_when_terminal_success_has_tool_only_ending(self) -> None:
        """Tool activity with no final text still counts as answered, and must
        not be confused with a cancellation."""
        lc, _sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())

        state = TurnState(
            content=[
                TextBlock(kind="text", text="I'll run that."),
                ToolUseBlock(
                    kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}
                ),
            ]
        )
        await lc.on_terminal_success(state)

        assert lc.was_answered is True, "visible tool activity must report an answer"
        assert edits, "the done embed flush must still land"
        assert all(e[1].get("content") != "Turn cancelled." for e in edits), (
            "a turn with tool activity must not be rendered as a cancellation"
        )

    async def test_was_answered_is_false_when_terminal_success_has_empty_content(self) -> None:
        """A cancelled turn -- no text, no tool activity -- must not report an
        answer, and the rendered message must agree."""
        lc, _sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        state = TurnState()  # completely empty content
        await lc.on_terminal_success(state)

        assert lc.was_answered is False, "a cancelled turn must not report an answer"
        cancel_edit = edits[-1]
        assert cancel_edit[1].get("content") == "Turn cancelled.", (
            "a cancelled turn must render as 'Turn cancelled.'"
        )

    async def test_was_answered_is_false_when_terminal_failure(self) -> None:
        """A failed turn must not report an answer."""
        lc, _sends, _edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        state = TurnState()
        await lc.on_terminal_failure(state, Exception("boom"))

        assert lc.was_answered is False, "a failed turn must not report an answer"


class TestTurnSummaryFooter:
    async def test_priced_model_sets_cost_str(self) -> None:
        lc, _sends, edits = _make_lifecycle(model_id="claude-sonnet-4-6")
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
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
        await lc.on_render(TurnState())
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
        await lc.on_render(TurnState())
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
        await lc.on_render(TurnState())
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


@pytest.mark.asyncio
async def test_adopted_message_ref_edits_instead_of_posting_a_second_message() -> None:
    """Dead-session recovery must reuse the failed attempt's message.

    Without this the recovery lifecycle posts a SECOND message and the first
    attempt's upstream-error embed is left standing in the thread — the user
    sees a red failure immediately followed by a working answer, with no way to
    tell the failure was retracted. Observed on staging after the recovery fix
    landed: the turn ran fine but the 400 embed stayed above it.
    """
    sent: list[object] = []
    edited: list[object] = []

    async def _send(**kwargs: object) -> object:
        sent.append(kwargs)
        return "new-message"

    async def _edit(ref: object, **kwargs: object) -> None:
        edited.append((ref, kwargs))

    lifecycle = DiscordTurnLifecycle(
        send=_send,
        edit=_edit,
        agent_name="content-daimon",
        model_id="claude-sonnet-5",
        adopt_message_ref="failed-attempt-message",
    )

    assert lifecycle.message_ref == "failed-attempt-message"

    await lifecycle.on_terminal_success(_make_success_state())

    assert sent == [], "must not post a second message when one was adopted"
    assert edited, "the recovered answer must be written somewhere"
    assert {ref for ref, _ in edited} == {"failed-attempt-message"}, (
        "every write must target the failed attempt's message, overwriting its error embed"
    )


# ---------------------------------------------------------------------------
# Unprompted turns: silent until the agent actually speaks
# ---------------------------------------------------------------------------


def _make_unprompted_lifecycle() -> tuple[
    DiscordTurnLifecycle, list[dict[str, Any]], list[tuple[Any, dict[str, Any]]], list[Any]
]:
    """Recorder lifecycle for an organic-thread-participation turn.

    Returns (lifecycle, sends, edits, deletes).
    """
    sends: list[dict[str, Any]] = []
    edits: list[tuple[Any, dict[str, Any]]] = []
    deletes: list[Any] = []

    async def fake_send(**kwargs: Any) -> object:
        sends.append(kwargs)
        return _SENTINEL_REF

    async def fake_edit(ref: Any, **kwargs: Any) -> None:
        edits.append((ref, kwargs))

    async def fake_delete(ref: Any) -> None:
        deletes.append(ref)

    lc = DiscordTurnLifecycle(
        send=fake_send,
        edit=fake_edit,
        delete=fake_delete,
        agent_name="test-agent",
        model_id="claude-sonnet-4-6",
        unprompted=True,
    )
    return lc, sends, edits, deletes


class TestUnpromptedTurn:
    async def test_post_initial_posts_nothing(self) -> None:
        """Nobody asked, so the thinking embed does not go up before the turn."""
        lc, sends, edits, _ = _make_unprompted_lifecycle()

        await lc.post_initial()

        assert sends == [] and edits == [], "an unprompted turn announces nothing up front"

    async def test_a_turn_that_ends_empty_leaves_nothing_behind(self) -> None:
        """No text and no tool activity: no embed, and no 'Turn cancelled.' notice."""
        lc, sends, edits, deletes = _make_unprompted_lifecycle()

        await lc.post_initial()
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        await lc.on_terminal_success(TurnState())

        assert sends == [], "thinking alone is not something to say"
        assert edits == [], "there is no embed to edit into 'Turn cancelled.'"
        assert deletes == [], "nothing was posted, so nothing needs deleting"
        assert lc.was_answered is False, "a silent turn did not answer"

    async def test_an_embed_posted_before_a_silent_end_is_deleted(self) -> None:
        """Text that streams and then vanishes (a cancel) takes its embed with it."""
        lc, sends, _, deletes = _make_unprompted_lifecycle()

        await lc.on_sse_event(_message_event("thinking out loud"))
        await lc.on_render(TurnState(content=[TextBlock(kind="text", text="partial")]))
        await lc.on_terminal_success(TurnState())

        assert len(sends) == 1, "the embed went up while there was content"
        assert deletes == [_SENTINEL_REF], "the embed is removed once the turn says nothing"
        assert lc.final_message_id is None, "a deleted embed is not a watermark"

    async def test_a_tool_trail_with_no_answer_is_removed_too(self) -> None:
        """Tools ran, nothing was said: a mention would keep the done embed, an
        unprompted turn deletes it, since nobody watched those tools run."""
        lc, sends, edits, deletes = _make_unprompted_lifecycle()
        tool_only = TurnState(
            content=[
                ToolUseBlock(
                    kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}
                )
            ]
        )

        await lc.on_render(tool_only)
        await lc.on_terminal_success(tool_only)

        assert len(sends) == 1, "the embed went up when the tool ran"
        assert deletes == [_SENTINEL_REF], "no final answer means the embed comes down"
        assert not any("content" in kwargs for kwargs in edits), "no 'done' state is left behind"
        assert lc.was_answered is False, "a tool trail is not an answer to an unasked question"

    async def test_the_embed_appears_once_content_arrives(self) -> None:
        """The first render carrying real output is what posts the embed."""
        lc, sends, _, _ = _make_unprompted_lifecycle()

        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        assert sends == [], "thinking is not content"

        await lc.on_render(
            TurnState(
                content=[
                    ToolUseBlock(
                        kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}
                    )
                ]
            )
        )

        assert len(sends) == 1, "tool activity is visible work, so the embed goes up"
        assert "embeds" in sends[0], "the post carries the activity embed"

    async def test_every_send_suppresses_the_notification(self) -> None:
        """`silent=True` is Discord's suppress-notification flag: no ping for a reply
        nobody asked for."""
        lc, sends, _, _ = _make_unprompted_lifecycle()

        await lc.on_render(TurnState(content=[TextBlock(kind="text", text="here it is")]))
        await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="x" * 3000)]))

        assert sends, "the turn spoke, so it posted"
        assert all(kwargs.get("silent") is True for kwargs in sends), (
            "every message an unprompted turn sends is silent"
        )

    async def test_a_mention_turn_keeps_sending_with_no_silent_flag(self) -> None:
        """The mention path is unchanged: no silent kwarg, embed up front."""
        lc, sends, _ = _make_lifecycle()

        await lc.post_initial()

        assert len(sends) == 1, "a mention still gets its thinking embed immediately"
        assert "silent" not in sends[0], "mention turns notify as they always have"


class TestDegradedTurnNotice:
    async def test_terminal_success_names_the_failed_mcp_server_under_the_reply(self) -> None:
        """#79: a reply produced after an MCP failure is delivered, with the
        dropped server named under it instead of a blank failure embed."""
        from daimon.core.turn.state import McpServerFailure

        lc, _sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        state = TurnState(
            content=[TextBlock(kind="text", text="Here is the board.")],
            mcp_failures=(
                McpServerFailure(
                    server_name="notion",
                    error_type="mcp_authentication_failed_error",
                    message="access forbidden",
                    retry_status="exhausted",
                ),
            ),
        )
        await lc.on_terminal_success(state)

        content = edits[-1][1]["content"]
        assert content.startswith("Here is the board."), "the reply itself comes first"
        assert "`notion`" in content, "the dropped server is named under the reply"
        assert lc.was_answered, "a degraded turn still counts as answered"

    async def test_tool_only_turn_posts_the_notice_on_its_own(self) -> None:
        """No reply to hang the notice under: it goes out as its own message."""
        from daimon.core.turn.state import McpServerFailure

        lc, sends, _edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        state = TurnState(
            content=[
                ToolUseBlock(
                    kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}
                )
            ],
            mcp_failures=(
                McpServerFailure(
                    server_name="notion",
                    error_type="mcp_authentication_failed_error",
                    message="access forbidden",
                    retry_status="exhausted",
                ),
            ),
        )
        await lc.on_terminal_success(state)

        notices = [s for s in sends if "`notion`" in str(s.get("content", ""))]
        assert len(notices) == 1, "the dropped server is named once, on its own line"


async def test_completion_ping_posts_fresh_answer_and_limits_mentions():
    lifecycle, sends, edits = _make_lifecycle(notify_on_completion=True)
    await lifecycle.post_initial()
    await lifecycle.on_terminal_success(_make_success_state("Done <@456> @everyone"))
    assert len(sends) == 2
    assert sends[-1]["content"] == "<@123>\nDone <@456> @everyone"
    mentions = sends[-1]["allowed_mentions"].to_dict()
    assert mentions["users"] == [123]
    assert "everyone" not in mentions["parse"]
    assert "roles" not in mentions["parse"]
    assert not any(e.get("content") == sends[-1]["content"] for _, e in edits)
    assert await lifecycle.prepend_revealed_answer("Recovered files.")
    assert edits[-1][1]["content"].startswith("Recovered files.")


@pytest.mark.parametrize("enabled", [False, True])
async def test_reactions_replace_accepted_after_success(enabled):
    calls = []

    class Trigger:
        guild = types.SimpleNamespace(me=object())

        async def add_reaction(self, emoji):
            calls.append(("add", emoji))

        async def remove_reaction(self, emoji, user):
            calls.append(("remove", emoji))

    async def send(**kwargs):
        return _SENTINEL_REF

    async def edit(ref, **kwargs):
        pass

    lifecycle = DiscordTurnLifecycle(
        send=send,
        edit=edit,
        agent_name="test",
        model_id="claude-sonnet-4-6",
        trigger_message=Trigger(),
        notify_on_completion=enabled,
    )
    await lifecycle.on_acknowledgment("accepted")
    await lifecycle.on_terminal_success(_make_success_state())
    await lifecycle.on_acknowledgment("done")
    assert calls == ([("add", "👀"), ("add", "✅"), ("remove", "👀")] if enabled else [])


async def test_completion_preserves_original_card_id():
    refs = iter([types.SimpleNamespace(id=1000), types.SimpleNamespace(id=1001)])

    async def send(**kwargs):
        return next(refs)

    async def edit(ref, **kwargs):
        pass

    lifecycle = DiscordTurnLifecycle(
        send=send,
        edit=edit,
        agent_name="test",
        model_id="claude-sonnet-4-6",
        requester_id=123,
        notify_on_completion=True,
    )
    await lifecycle.post_initial()
    await lifecycle.on_terminal_success(_make_success_state())
    assert lifecycle.card_message_id == "1000"
    assert lifecycle.final_message_id == "1001"


@pytest.mark.parametrize("notify", [False, True])
async def test_final_table_is_attached_to_answer(notify):
    lifecycle, sends, edits = _make_lifecycle(render_tables=True, notify_on_completion=notify)
    await lifecycle.post_initial()
    await lifecycle.on_terminal_success(
        _make_success_state("| Name | Value |\n| --- | ---: |\n| Example | 42 |")
    )
    answer = sends[-1] if notify else edits[-1][1]
    attachment_key = "files" if notify else "attachments"
    assert "table-1.png" in answer["content"]
    assert "| ---" not in answer["content"]
    assert len(answer[attachment_key]) == 1
    assert answer[attachment_key][0].fp.read(8) == b"\x89PNG\r\n\x1a\n"
    assert len(sends) == (2 if notify else 1)


@pytest.mark.parametrize("status", [403, 413, 500])
@pytest.mark.parametrize("notify", [False, True])
async def test_rejected_table_upload_retries_original_answer_as_text(status, notify):
    from daimon.adapters.discord.split import split_for_discord_safe
    from structlog.testing import capture_logs

    delivered = []
    attempts = []
    ref = types.SimpleNamespace(id=1000)

    def reject_upload(kwargs):
        attempts.append(kwargs)
        if kwargs.get("attachments") or kwargs.get("files"):
            response = types.SimpleNamespace(status=status, reason="Rejected upload")
            error = discord.Forbidden if status == 403 else discord.HTTPException
            raise error(response, "upload rejected")

    async def send(**kwargs):
        reject_upload(kwargs)
        if "content" in kwargs:
            delivered.append(kwargs)
        return ref

    async def edit(message, **kwargs):
        reject_upload(kwargs)
        if "content" in kwargs:
            delivered.append(kwargs)

    lifecycle = DiscordTurnLifecycle(
        send=send,
        edit=edit,
        render_tables=True,
        agent_name="test",
        model_id="claude-sonnet-4-6",
        notify_on_completion=notify,
        requester_id=123,
    )
    lifecycle.answer_prefix = "Recovered files."
    text = "| Name | Value |\n| --- | ---: |\n| Example | 42 |\n\n" + "Tail. " * 500
    await lifecycle.post_initial()
    with capture_logs() as logs:
        await lifecycle.on_terminal_success(_make_success_state(text))
    assert [part["content"] for part in delivered] == split_for_discord_safe(
        ("<@123>\n" if notify else "") + "Recovered files.\n\n" + text
    )
    assert all("attachments" not in part and "files" not in part for part in delivered)
    assert all(part["allowed_mentions"].to_dict()["parse"] == [] for part in delivered)
    assert (
        sum(bool(attempt.get("attachments") or attempt.get("files")) for attempt in attempts) == 1
    )
    assert delivered[0]["allowed_mentions"].to_dict().get("users", []) == ([123] if notify else [])
    assert any(entry["event"] == "turn.table_delivery_failed" for entry in logs)
    assert lifecycle.was_answered
