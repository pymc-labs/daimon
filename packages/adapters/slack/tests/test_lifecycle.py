"""Tests for SlackTurnLifecycle (lifecycle.py).

Behavioral assertions — grouped by phase:

Task 1 (debounce / registry / usage):
  - First SSE event triggers exactly one chat.postMessage and zero chat.update.
  - Second SSE event within 5s triggers NO chat.update (debounce window).
  - SSE event after 5s triggers exactly one chat.update (debounce elapsed).
  - First flush registers status_ts in the injected registry as (cancel Event, author_id).
  - _apply_usage folds usage_totals into merged usage_in / usage_out / cost_str.

Task 2 (terminal paths — replace-in-place, overflow, collapse, failure, deregister):
  - on_terminal_success with text replaces status message in place (chat.update on status_ts).
  - Long text posts overflow chunks via chat.postMessage; final_ts = LAST posted ts (D-06).
  - Tool-only turn (no final text) leaves the collapsed done; no overflow post.
  - Empty content (no blocks at all) updates status message to 'Turn cancelled.'
  - on_terminal_failure posts/updates error state and does NOT raise.
  - registry deregister callback is invoked for status_ts in the terminal finally (D-01).
  - SlackTurnLifecycle satisfies the TurnLifecycle Protocol.

Transport-level fake via aioresponses (guideline:testing) — transport-level fakes only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import types
from typing import Any

import yarl
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.state import (
    TextBlock,
    ToolUseBlock,
    TurnState,
    UsageTotals,
)

# ---------------------------------------------------------------------------
# URL constants for mock.requests inspection
# ---------------------------------------------------------------------------

_POST_URL = yarl.URL("https://slack.com/api/chat.postMessage")
_UPDATE_URL = yarl.URL("https://slack.com/api/chat.update")


def _post_count(fake: Any) -> int:
    """Count chat.postMessage calls made against the mock."""
    return len(fake.mock.requests.get(("POST", _POST_URL), []))


def _update_count(fake: Any) -> int:
    """Count chat.update calls made against the mock."""
    return len(fake.mock.requests.get(("POST", _UPDATE_URL), []))


def _last_update_blocks(fake: Any) -> list[dict[str, Any]]:
    """Block list from the body of the most recent chat.update request."""
    calls = fake.mock.requests.get(("POST", _UPDATE_URL), [])
    assert calls, "expected at least one chat.update call"
    return calls[-1].kwargs["json"]["blocks"]


def _has_actions_block(blocks: list[dict[str, Any]]) -> bool:
    """True if any block is an actions block (i.e. the cancel button is present)."""
    return any(b.get("type") == "actions" for b in blocks)


def _block_text(blocks: list[dict[str, Any]]) -> str:
    """Flatten all rendered text in a block list for substring assertions."""
    parts: list[str] = []
    for b in blocks:
        if isinstance(b.get("text"), dict):
            parts.append(b["text"].get("text", ""))
        elif isinstance(b.get("text"), str):
            parts.append(b["text"])
        for el in b.get("elements", []):
            if isinstance(el, dict) and isinstance(el.get("text"), str):
                parts.append(el["text"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# SSE event constructors (SimpleNamespace fakes — same pattern as Discord tests)
# ---------------------------------------------------------------------------


def _thinking_event() -> Any:
    """MA session SSE event: agent.thinking."""
    return types.SimpleNamespace(type="agent.thinking")


def _tool_event(name: str = "Bash") -> Any:
    """MA session SSE event: agent.tool_use."""
    return types.SimpleNamespace(type="agent.tool_use", name=name)


# ---------------------------------------------------------------------------
# Lifecycle factory
# ---------------------------------------------------------------------------


def _make_lifecycle(
    fake: Any,
    *,
    model_id: str = "claude-sonnet-4-6",
    agent_name: str = "test-agent",
) -> tuple[SlackTurnLifecycle, asyncio.Event, dict[str, tuple[asyncio.Event, str]], list[str]]:
    """Create a SlackTurnLifecycle with recorder callables for registry operations.

    Returns:
        (lifecycle, cancel_event, registered_dict, deregistered_list)
        - registered_dict maps ts -> (cancel_event, author_id) on each register call.
        - deregistered_list accumulates ts values on each deregister call.
    """
    cancel = asyncio.Event()
    registered: dict[str, tuple[asyncio.Event, str]] = {}
    deregistered: list[str] = []

    def register(ts: str, ev: asyncio.Event, author_id: str) -> None:
        registered[ts] = (ev, author_id)

    def deregister(ts: str) -> None:
        deregistered.append(ts)

    lc = SlackTurnLifecycle(
        client=fake.client,
        channel="C_TEST",
        thread_ts="1700000000.000000",
        cancel=cancel,
        author_id="U_AUTHOR",
        agent_name=agent_name,
        model_id=model_id,
        register=register,
        deregister=deregister,
    )
    return lc, cancel, registered, deregistered


# ---------------------------------------------------------------------------
# Task 1: Debounce
# ---------------------------------------------------------------------------


async def test_first_sse_event_posts_immediately(fake_slack_web_client: Any) -> None:
    """First SSE event triggers exactly one chat.postMessage and zero chat.update."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)

    await lc.on_sse_event(_thinking_event())

    assert _post_count(fake_slack_web_client) == 1, (
        "first SSE event must trigger exactly one chat.postMessage (immediate flush)"
    )
    assert _update_count(fake_slack_web_client) == 0, (
        "no chat.update on the first event — status message not yet established"
    )


async def test_second_event_within_debounce_no_update(fake_slack_web_client: Any) -> None:
    """Second SSE event within 5s of the first triggers NO chat.update (debounce window)."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)

    await lc.on_sse_event(_thinking_event())
    await lc.on_sse_event(_tool_event())  # within debounce window (no time elapsed)

    assert _post_count(fake_slack_web_client) == 1, (
        "second event within debounce must not post a new message"
    )
    assert _update_count(fake_slack_web_client) == 0, "no chat.update within the 5s debounce window"


async def test_event_after_debounce_triggers_update(fake_slack_web_client: Any) -> None:
    """SSE event after 5s debounce window triggers exactly one chat.update."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)

    await lc.on_sse_event(_thinking_event())  # initial post
    # Backdate _last_flush to simulate 6s elapsed (established idiom from Discord tests)
    lc._last_flush = time.monotonic() - 6.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce

    await lc.on_sse_event(_tool_event())

    assert _post_count(fake_slack_web_client) == 1, "debounced update must NOT post a new message"
    assert _update_count(fake_slack_web_client) == 1, (
        "exactly one chat.update after debounce window elapses"
    )


# ---------------------------------------------------------------------------
# Task 1: Registry
# ---------------------------------------------------------------------------


async def test_first_flush_registers_status_ts(fake_slack_web_client: Any) -> None:
    """First flush registers status_ts with (cancel Event, author_id) in the registry (D-01)."""
    lc, cancel, registered, _ = _make_lifecycle(fake_slack_web_client)

    await lc.on_sse_event(_thinking_event())

    assert len(registered) == 1, "exactly one registration after the first flush"
    ts, (reg_event, reg_author) = next(iter(registered.items()))
    assert ts == "1000000000.000001", (
        "registered ts must match the ts from the chat.postMessage response"
    )
    assert reg_event is cancel, "registered cancel event must be the one injected at construction"
    assert reg_author == "U_AUTHOR", "registered author_id must match the constructor arg"


# ---------------------------------------------------------------------------
# Task 1: Usage footer
# ---------------------------------------------------------------------------


async def test_apply_usage_folds_usage_totals(fake_slack_web_client: Any) -> None:
    """_apply_usage folds usage_totals (merged input + output + cost_str) onto lifecycle state."""
    lc, *_ = _make_lifecycle(fake_slack_web_client, model_id="claude-sonnet-4-6")
    state = dataclasses.replace(
        TurnState(),
        usage_totals=UsageTotals(
            input_tokens=1000,
            cache_creation_input_tokens=500,
            cache_read_input_tokens=2000,
            output_tokens=300,
        ),
    )

    lc._apply_usage(state)  # pyright: ignore[reportPrivateUsage]  # unit-testing internal helper

    # merged_in = 1000 + 500 + 2000 = 3500
    assert lc._state.usage_in == 3500, (  # pyright: ignore[reportPrivateUsage]
        "usage_in must be the merged input (input + cache_creation + cache_read)"
    )
    assert lc._state.usage_out == 300, (  # pyright: ignore[reportPrivateUsage]
        "usage_out must equal output_tokens"
    )

    expected_cost = format_cost(
        cost_of(
            BetaManagedAgentsSpanModelUsage(
                input_tokens=1000,
                cache_creation_input_tokens=500,
                cache_read_input_tokens=2000,
                output_tokens=300,
                speed="standard",
            ),
            MODEL_PRICING["claude-sonnet-4-6"],
        )
    )
    assert lc._state.cost_str == expected_cost, (  # pyright: ignore[reportPrivateUsage]
        "cost_str must match the billing-ledger cost to the cent"
    )


# ---------------------------------------------------------------------------
# Task 2: Terminal — replace in place
# ---------------------------------------------------------------------------


async def test_terminal_success_replaces_status_in_place(fake_slack_web_client: Any) -> None:
    """on_terminal_success with text replaces the status message via chat.update on status_ts.

    First chunk is placed via chat.update (not a new chat.postMessage), so final_ts = status_ts.
    """
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())  # initial post (1 postMessage)

    state = TurnState(content=[TextBlock(kind="text", text="The answer is 42.")])
    await lc.on_terminal_success(state)

    # No overflow post — first chunk replaces the status message in place (chat.update)
    assert _post_count(fake_slack_web_client) == 1, (
        "non-overflow success must not add a new postMessage beyond the initial status"
    )
    assert lc.final_ts == "1000000000.000001", (
        "final_ts must equal status_ts when there is no overflow"
    )

    blocks = _last_update_blocks(fake_slack_web_client)
    assert blocks[0]["type"] == "markdown", "final answer must render as a native markdown block"
    assert "The answer is 42." in blocks[0]["text"], "first block must carry the answer text"
    assert any(b["type"] == "context" for b in blocks), (
        "terminal message must include the cost/usage footer context block (D-08)"
    )
    assert not _has_actions_block(blocks), (
        "cancel button must be removed on terminal success (D-05)"
    )


async def test_terminal_success_overflow_posts_and_widens_final_ts(
    fake_slack_web_client: Any,
) -> None:
    """Long text splits into overflow chunks posted via chat.postMessage; final_ts = LAST ts (D-06)."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())
    initial_posts = _post_count(fake_slack_web_client)

    long_text = "x" * 24000  # three chunks at _SLACK_LIMIT=11800
    state = TurnState(content=[TextBlock(kind="text", text=long_text)])
    await lc.on_terminal_success(state)

    overflow_posts = _post_count(fake_slack_web_client) - initial_posts
    assert overflow_posts >= 1, "overflow chunks must be posted as new chat.postMessage calls"
    assert lc.final_ts is not None, "final_ts must be set after overflow"


# ---------------------------------------------------------------------------
# Task 2: Terminal — collapse paths (tool-only / cancelled)
# ---------------------------------------------------------------------------


async def test_terminal_success_tool_only_leaves_collapsed_done(
    fake_slack_web_client: Any,
) -> None:
    """Tool-only turn (no final text after last ToolUseBlock) leaves the collapsed done; no overflow."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())
    initial_posts = _post_count(fake_slack_web_client)

    state = TurnState(
        content=[
            TextBlock(kind="text", text="I'll run that."),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
        ]
    )
    await lc.on_terminal_success(state)

    assert _post_count(fake_slack_web_client) == initial_posts, (
        "tool-only turn must not post any overflow — collapsed done stays"
    )
    assert lc.final_ts == "1000000000.000001", "final_ts must equal status_ts for tool-only turn"

    blocks = _last_update_blocks(fake_slack_web_client)
    assert not _has_actions_block(blocks), (
        "tool-only terminal must collapse to the done footer with no cancel button"
    )
    assert any(b["type"] == "context" for b in blocks), (
        "tool-only terminal must render the done footer context block"
    )
    assert not any(b.get("type") == "markdown" for b in blocks), (
        "tool-only turn has no final answer — no markdown answer block"
    )


async def test_terminal_success_empty_content_shows_turn_cancelled(
    fake_slack_web_client: Any,
) -> None:
    """Empty content (no blocks) triggers a chat.update with 'Turn cancelled.'."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState()  # completely empty
    await lc.on_terminal_success(state)

    assert _update_count(fake_slack_web_client) >= 1, (
        "cancelled turn must trigger at least one chat.update"
    )
    assert lc.final_ts is not None, "final_ts must be set even for cancelled turns"

    blocks = _last_update_blocks(fake_slack_web_client)
    assert "Turn cancelled." in _block_text(blocks), (
        "an empty turn (no text, no tools) must render 'Turn cancelled.'"
    )
    assert not _has_actions_block(blocks), "cancelled turn must not keep the cancel button"


# ---------------------------------------------------------------------------
# Task 2: Terminal — failure
# ---------------------------------------------------------------------------


async def test_terminal_failure_does_not_raise(fake_slack_web_client: Any) -> None:
    """on_terminal_failure updates/posts error state and does NOT raise."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState()
    err = RuntimeError("upstream blew up")

    # Must not raise — lifecycle boundary never re-raises
    await lc.on_terminal_failure(state, err)

    blocks = _last_update_blocks(fake_slack_web_client)
    text = _block_text(blocks)
    assert "❌" in text and "upstream blew up" in text, (
        "terminal failure must render the ❌ error footer with the failure reason"
    )
    assert not _has_actions_block(blocks), "error terminal must drop the cancel button (D-05)"


# ---------------------------------------------------------------------------
# Task 2: Terminal — registry deregister in finally (D-01)
# ---------------------------------------------------------------------------


async def test_deregister_called_in_terminal_success_finally(
    fake_slack_web_client: Any,
) -> None:
    """deregister is invoked for status_ts in the on_terminal_success finally block (D-01)."""
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState(content=[TextBlock(kind="text", text="Hello.")])
    await lc.on_terminal_success(state)

    assert len(deregistered) == 1, "deregister must be called exactly once"
    assert deregistered[0] == "1000000000.000001", "deregistered ts must match status_ts"


async def test_deregister_called_in_terminal_failure_finally(
    fake_slack_web_client: Any,
) -> None:
    """deregister is invoked for status_ts in the on_terminal_failure finally block (D-01)."""
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState()
    await lc.on_terminal_failure(state, RuntimeError("boom"))

    assert len(deregistered) == 1, "deregister must be called exactly once in failure path"
    assert deregistered[0] == "1000000000.000001", "deregistered ts must match status_ts"


# ---------------------------------------------------------------------------
# Task 2: Protocol conformance
# ---------------------------------------------------------------------------


async def test_lifecycle_satisfies_turn_lifecycle_protocol(fake_slack_web_client: Any) -> None:
    """SlackTurnLifecycle is assignable to TurnLifecycle protocol (SREND-01)."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    bound: TurnLifecycle = lc  # type annotation asserts protocol conformance
    assert callable(bound.on_render), "on_render must be callable"
    assert callable(bound.on_terminal_success), "on_terminal_success must be callable"
    assert callable(bound.on_terminal_failure), "on_terminal_failure must be callable"
    assert callable(bound.on_sse_event), "on_sse_event must be callable"
    assert callable(bound.on_reconnect), "on_reconnect must be callable"
    assert callable(bound.on_rate_limited), "on_rate_limited must be callable"
    assert callable(bound.on_interrupt_sent), "on_interrupt_sent must be callable"
