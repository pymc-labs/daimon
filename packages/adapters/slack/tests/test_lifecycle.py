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
  - Long text posts overflow chunks via chat.postMessage; final_ts = LAST posted ts.
  - Tool-only turn (no final text) leaves the collapsed done; no overflow post.
  - Empty content (no blocks at all) updates status message to 'Turn cancelled.'
  - on_terminal_failure posts/updates error state and does NOT raise.
  - registry deregister callback is invoked for status_ts in the terminal finally.
  - SlackTurnLifecycle satisfies the TurnLifecycle Protocol.

Terminal flush failure — best-effort repair (#107):
  - A failed answer-replace chat.update collapses the status to a plain failure
    notice instead of leaving the live surface (phase, tool trail, dead cancel).
  - A failed repair is swallowed — on_terminal_success never raises.
  - A failed first post (no status message) attempts no repair.
  - An overflow-post failure does NOT clobber the already-replaced answer.
  - final_ts stays None on every flush failure so the watermark cannot advance
    past an answer the user never saw.
  - on_terminal_failure's own flush failure gets the same repair.
  - Transport errors (aiohttp, not SlackApiError) get the same repair, and
    neither terminal hook raises when flush AND repair both hit them.
  - A no-answer collapse is repaired with copy that does not claim an answer.
  - The swallowed flush failure is captured to Sentry (answer-delivery outage).

Transport-level fake via aioresponses (guideline:testing) — transport-level fakes only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import types
from typing import Any

import aiohttp
import pytest
import yarl
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.slack import lifecycle as lifecycle_mod
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.state import (
    TextBlock,
    ToolUseBlock,
    TurnState,
    UsageTotals,
)
from slack_sdk.errors import SlackApiError

from .conftest import CHAT_OK_PAYLOAD

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


def _action_ids(blocks: list[dict[str, Any]]) -> list[str]:
    """All action_ids across every actions block, in render order."""
    return [
        el["action_id"]
        for b in blocks
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    ]


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
    """First flush registers status_ts with (cancel Event, author_id) in the registry."""
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
        "terminal message must include the cost/usage footer context block"
    )
    assert "cancel_turn" not in _action_ids(blocks), (
        "cancel button must be removed on terminal success"
    )


async def test_terminal_success_overflow_posts_and_widens_final_ts(
    fake_slack_web_client: Any,
) -> None:
    """Long text splits into overflow chunks posted via chat.postMessage; final_ts = LAST ts."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())
    initial_posts = _post_count(fake_slack_web_client)

    long_text = "x" * 24000  # three chunks at _SLACK_LIMIT=11800
    state = TurnState(content=[TextBlock(kind="text", text=long_text)])
    await lc.on_terminal_success(state)

    overflow_posts = _post_count(fake_slack_web_client) - initial_posts
    assert overflow_posts >= 1, "overflow chunks must be posted as new chat.postMessage calls"
    assert lc.final_ts is not None, "final_ts must be set after overflow"


async def test_terminal_success_bounds_notification_text_on_long_answers(
    fake_slack_web_client: Any,
) -> None:
    """The `text` notification fallback stays bounded while blocks carry the full chunk.

    chat.update rejects a message whose `text` is block-sized with msg_too_long
    (probed live: update with text=11800 fails where chat.postMessage accepts the
    identical payload), which lost a completed 28k-char answer entirely — the
    status message stayed stuck at thinking with a dead cancel button. The
    rendered content lives in the markdown block; `text` only feeds
    notifications, so it must never grow with the answer.
    """
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    long_text = "y" * 24000  # forces chunks at the 11800 block limit
    state = TurnState(content=[TextBlock(kind="text", text=long_text)])
    await lc.on_terminal_success(state)

    update_calls = fake_slack_web_client.mock.requests.get(("POST", _UPDATE_URL), [])
    assert update_calls, "the first chunk must replace the status message via chat.update"
    update_body = update_calls[-1].kwargs["json"]
    assert len(update_body["text"]) <= 3000, (
        "chat.update's notification fallback must stay under the update text limit "
        "(msg_too_long above ~4000)"
    )
    assert len(update_body["blocks"][0]["text"]) > 3000, (
        "the markdown block must still carry the full first chunk — only the "
        "notification fallback is bounded"
    )

    post_calls = fake_slack_web_client.mock.requests.get(("POST", _POST_URL), [])
    overflow_bodies = [
        c.kwargs["json"]
        for c in post_calls
        if c.kwargs.get("json", {}).get("blocks", [{}])[0].get("type") == "markdown"
    ]
    assert overflow_bodies, "overflow chunks must exist for a 24k answer"
    assert all(len(b["text"]) <= 3000 for b in overflow_bodies), (
        "overflow chunks' notification fallbacks must be bounded too"
    )


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
    assert not _has_actions_block(blocks), "error terminal must drop the cancel button"


# ---------------------------------------------------------------------------
# Task 2: Terminal — registry deregister in finally
# ---------------------------------------------------------------------------


async def test_deregister_called_in_terminal_success_finally(
    fake_slack_web_client: Any,
) -> None:
    """deregister is invoked for status_ts in the on_terminal_success finally block."""
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState(content=[TextBlock(kind="text", text="Hello.")])
    await lc.on_terminal_success(state)

    assert len(deregistered) == 1, "deregister must be called exactly once"
    assert deregistered[0] == "1000000000.000001", "deregistered ts must match status_ts"


async def test_deregister_called_in_terminal_failure_finally(
    fake_slack_web_client: Any,
) -> None:
    """deregister is invoked for status_ts in the on_terminal_failure finally block."""
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState()
    await lc.on_terminal_failure(state, RuntimeError("boom"))

    assert len(deregistered) == 1, "deregister must be called exactly once in failure path"
    assert deregistered[0] == "1000000000.000001", "deregistered ts must match status_ts"


# ---------------------------------------------------------------------------
# Terminal flush failure — best-effort repair (#107)
# ---------------------------------------------------------------------------


def _reset_slack_responses(fake: Any) -> None:
    """Drop the fixture's repeat=True ok defaults so failures can be staged.

    aioresponses matches in registration order, so the fixture's defaults
    (registered first) would otherwise always win over per-test overrides.
    """
    fake.mock.clear()


def _stage_flush_failure_then_repair_ok(fake: Any, *, error: str) -> None:
    """Posts succeed; the FIRST chat.update fails with ``error``; later updates succeed.

    The shape shared by every repair test: the initial SSE flush posts fine,
    the terminal flush's update raises, and the repair update lands.
    """
    _reset_slack_responses(fake)
    fake.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_POST_URL),
        payload=CHAT_OK_PAYLOAD,
        repeat=True,
    )
    fake.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_UPDATE_URL),
        payload={"ok": False, "error": error},
    )
    fake.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_UPDATE_URL),
        payload=CHAT_OK_PAYLOAD,
        repeat=True,
    )


async def test_terminal_success_flush_failure_repairs_status_message(
    fake_slack_web_client: Any,
) -> None:
    """A failed answer-replace chat.update collapses the status to a failure notice.

    Without the repair the status message keeps whatever the last debounced
    flush wrote — phase title, tool trail, cancel button — while the cancel
    Event is deregistered in finally, so the turn looks alive forever with a
    dead control.
    """
    _stage_flush_failure_then_repair_ok(fake_slack_web_client, error="msg_too_long")
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState(content=[TextBlock(kind="text", text="The answer is 42.")])
    await lc.on_terminal_success(state)  # must not raise

    assert _update_count(fake_slack_web_client) == 2, (
        "the failed answer replace must be followed by exactly one repair chat.update"
    )
    blocks = _last_update_blocks(fake_slack_web_client)
    assert "went wrong" in _block_text(blocks), (
        "the repair must render a plain failure notice, not the live surface"
    )
    assert not _has_actions_block(blocks), "the repair must not keep the dead cancel button"
    assert lc.final_ts is None, (
        "final_ts must stay unset — the watermark must not advance past an answer "
        "the user never saw"
    )
    assert deregistered == ["1000000000.000001"], (
        "deregister must still run exactly once for status_ts"
    )


async def test_terminal_success_swallows_repair_failure(
    fake_slack_web_client: Any,
) -> None:
    """When the repair chat.update fails for the same reason, nothing propagates."""
    _reset_slack_responses(fake_slack_web_client)
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_POST_URL),
        payload=CHAT_OK_PAYLOAD,
        repeat=True,
    )
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_UPDATE_URL),
        payload={"ok": False, "error": "token_revoked"},
        repeat=True,
    )
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState(content=[TextBlock(kind="text", text="Hello.")])
    await lc.on_terminal_success(state)  # must not raise

    assert _update_count(fake_slack_web_client) == 2, (
        "exactly one repair attempt after the failed flush — no retry loop"
    )
    assert lc.final_ts is None, "final_ts must stay unset when nothing posted"
    assert deregistered == ["1000000000.000001"], "deregister must still run in finally"


async def test_terminal_success_first_post_failure_attempts_no_repair(
    fake_slack_web_client: Any,
) -> None:
    """A turn reaching terminal with no status message has nothing to repair.

    The terminal flush is the FIRST post (no prior SSE flush) and it fails:
    status_ts is None, so there is no stranded message and no repair target.
    """
    _reset_slack_responses(fake_slack_web_client)
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_POST_URL),
        payload={"ok": False, "error": "channel_not_found"},
        repeat=True,
    )
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)

    state = TurnState(content=[TextBlock(kind="text", text="Hello.")])
    await lc.on_terminal_success(state)  # must not raise

    assert _update_count(fake_slack_web_client) == 0, (
        "no chat.update may be attempted when no status message exists"
    )
    assert lc.final_ts is None, "final_ts must stay unset when nothing posted"
    assert deregistered == [], "nothing was registered, so nothing to deregister"


async def test_terminal_success_overflow_failure_keeps_replaced_answer(
    fake_slack_web_client: Any,
) -> None:
    """An overflow-post failure must not clobber the already-replaced answer.

    The first chunk landed via chat.update, so the status message shows real
    answer text; repairing it to a failure notice would destroy content the
    user can already read. final_ts still stays None so the watermark does not
    advance past the missing tail.
    """
    _reset_slack_responses(fake_slack_web_client)
    # Initial SSE flush posts fine; every later post (the overflow chunks) fails.
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_POST_URL),
        payload=CHAT_OK_PAYLOAD,
    )
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_POST_URL),
        payload={"ok": False, "error": "ratelimited"},
        repeat=True,
    )
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_UPDATE_URL),
        payload=CHAT_OK_PAYLOAD,
        repeat=True,
    )
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    long_text = "x" * 24000  # forces overflow chunks past the first update
    state = TurnState(content=[TextBlock(kind="text", text=long_text)])
    await lc.on_terminal_success(state)  # must not raise

    assert _update_count(fake_slack_web_client) == 1, (
        "the successful answer replace must stand — no repair update over it"
    )
    blocks = _last_update_blocks(fake_slack_web_client)
    assert blocks[0]["type"] == "markdown" and "x" in blocks[0]["text"], (
        "the status message must keep the first answer chunk"
    )
    assert lc.final_ts is None, "final_ts must stay unset when overflow chunks failed to post"
    assert deregistered == ["1000000000.000001"], "deregister must still run in finally"


async def test_terminal_failure_flush_failure_repairs_status_message(
    fake_slack_web_client: Any,
) -> None:
    """on_terminal_failure's own flush failure gets the same repair treatment."""
    _stage_flush_failure_then_repair_ok(fake_slack_web_client, error="msg_too_long")
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    await lc.on_terminal_failure(TurnState(), RuntimeError("upstream blew up"))

    assert _update_count(fake_slack_web_client) == 2, (
        "the failed error flush must be followed by exactly one repair chat.update"
    )
    blocks = _last_update_blocks(fake_slack_web_client)
    assert "went wrong" in _block_text(blocks), (
        "the repair must render a plain failure notice, not the live surface"
    )
    assert not _has_actions_block(blocks), "the repair must not keep the dead cancel button"
    assert deregistered == ["1000000000.000001"], "deregister must still run in finally"


async def test_terminal_success_transport_error_repairs_and_does_not_raise(
    fake_slack_web_client: Any,
) -> None:
    """A transport error during the answer replace gets the same repair as ok:false.

    slack_sdk re-raises aiohttp errors unwrapped — they never become
    SlackApiError — and the mention boundary's catch tuple does not include
    them, so an escape here surfaces as an unhandled task error while the
    status message stays stranded on the live surface.
    """
    _reset_slack_responses(fake_slack_web_client)
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_POST_URL),
        payload=CHAT_OK_PAYLOAD,
        repeat=True,
    )
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_UPDATE_URL),
        exception=aiohttp.ClientConnectionError("network down"),
    )
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_UPDATE_URL),
        payload=CHAT_OK_PAYLOAD,
        repeat=True,
    )
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState(content=[TextBlock(kind="text", text="Hello.")])
    await lc.on_terminal_success(state)  # must not raise

    blocks = _last_update_blocks(fake_slack_web_client)
    assert "went wrong" in _block_text(blocks), (
        "a transport error must produce the same repair notice as an ok:false response"
    )
    assert lc.final_ts is None, "final_ts must stay unset when the answer never posted"
    assert deregistered == ["1000000000.000001"], "deregister must still run in finally"


async def test_terminal_failure_transport_error_in_flush_and_repair_does_not_raise(
    fake_slack_web_client: Any,
) -> None:
    """on_terminal_failure never raises, even when flush AND repair hit transport errors.

    The driver awaits this hook unguarded on every failure path; an escape
    aborts run_prepared_turn before its outcome bookkeeping.
    """
    _reset_slack_responses(fake_slack_web_client)
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_POST_URL),
        payload=CHAT_OK_PAYLOAD,
        repeat=True,
    )
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_UPDATE_URL),
        exception=aiohttp.ClientConnectionError("network down"),
        repeat=True,
    )
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    await lc.on_terminal_failure(TurnState(), RuntimeError("upstream blew up"))

    assert deregistered == ["1000000000.000001"], "deregister must still run in finally"


async def test_terminal_success_cancelled_collapse_repair_does_not_claim_an_answer(
    fake_slack_web_client: Any,
) -> None:
    """The repair after a failed 'Turn cancelled.' collapse must not mention an answer.

    A user who cancelled their own turn would otherwise read
    'Something went wrong posting the answer.' for a turn that produced none.
    """
    _stage_flush_failure_then_repair_ok(fake_slack_web_client, error="ratelimited")
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    await lc.on_terminal_success(TurnState())  # empty content — cancelled path

    text = _block_text(_last_update_blocks(fake_slack_web_client))
    assert "went wrong" in text, "the failed collapse must still be repaired"
    assert "answer" not in text, "a turn with no answer must not be described as one"
    assert deregistered == ["1000000000.000001"], "deregister must still run in finally"


async def test_terminal_success_flush_failure_reaches_sentry(
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swallowed terminal-flush failure still produces an ops signal.

    Before the repair existed, the exception reached the listener boundary's
    log.error + capture_exception_with_scope. Absorbing it in the lifecycle
    must not trade the stuck spinner for a silent answer-delivery outage —
    a tenant can bill tokens on every turn while every answer is dropped.
    """
    captured: list[BaseException] = []
    monkeypatch.setattr(lifecycle_mod, "capture_exception_with_scope", captured.append)
    _stage_flush_failure_then_repair_ok(fake_slack_web_client, error="msg_too_long")
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState(content=[TextBlock(kind="text", text="Hello.")])
    await lc.on_terminal_success(state)

    assert len(captured) == 1, "the flush failure must be captured exactly once"
    assert isinstance(captured[0], SlackApiError), "the captured exception is the flush failure"


# ---------------------------------------------------------------------------
# Task 2: Protocol conformance
# ---------------------------------------------------------------------------


async def test_lifecycle_satisfies_turn_lifecycle_protocol(fake_slack_web_client: Any) -> None:
    """SlackTurnLifecycle is assignable to TurnLifecycle protocol."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    bound: TurnLifecycle = lc  # type annotation asserts protocol conformance
    assert callable(bound.on_render), "on_render must be callable"
    assert callable(bound.on_terminal_success), "on_terminal_success must be callable"
    assert callable(bound.on_terminal_failure), "on_terminal_failure must be callable"
    assert callable(bound.on_sse_event), "on_sse_event must be callable"
    assert callable(bound.on_reconnect), "on_reconnect must be callable"
    assert callable(bound.on_rate_limited), "on_rate_limited must be callable"
    assert callable(bound.on_interrupt_sent), "on_interrupt_sent must be callable"


# ---------------------------------------------------------------------------
# Task 3: Feedback vote buttons on the final answer
# ---------------------------------------------------------------------------


async def test_terminal_success_appends_feedback_buttons(fake_slack_web_client: Any) -> None:
    """An answered turn carries the 👍/👎 vote buttons on the final message."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState(content=[TextBlock(kind="text", text="The answer is 42.")])
    await lc.on_terminal_success(state)

    ids = _action_ids(_last_update_blocks(fake_slack_web_client))
    assert "feedback_vote:up" in ids and "feedback_vote:down" in ids, (
        "answered turn must render the two feedback vote buttons"
    )


async def test_overflow_puts_feedback_buttons_on_last_chunk_only(
    fake_slack_web_client: Any,
) -> None:
    """With overflow, only the LAST posted chunk carries the vote buttons.

    The buttons must sit on the message final_ts points at, so a vote's
    message_id keys the same message the watermark does.
    """
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    long_text = "x" * 24000  # three chunks at _SLACK_LIMIT=11800
    state = TurnState(content=[TextBlock(kind="text", text=long_text)])
    await lc.on_terminal_success(state)

    assert "feedback_vote:up" not in _action_ids(_last_update_blocks(fake_slack_web_client)), (
        "the in-place first chunk must NOT carry vote buttons when overflow follows"
    )
    posts = fake_slack_web_client.mock.requests.get(("POST", _POST_URL), [])
    overflow_bodies = [c.kwargs["json"] for c in posts[1:]]  # posts[0] is the status message
    assert len(overflow_bodies) >= 2, "expected at least two overflow chunks"
    for body in overflow_bodies[:-1]:
        assert "feedback_vote:up" not in _action_ids(body["blocks"]), (
            "intermediate overflow chunks must not carry vote buttons"
        )
    assert "feedback_vote:up" in _action_ids(overflow_bodies[-1]["blocks"]), (
        "the last overflow chunk must carry the vote buttons"
    )


async def test_tool_only_turn_gets_no_feedback_buttons(fake_slack_web_client: Any) -> None:
    """A turn with no final answer text must not invite feedback on it."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.on_sse_event(_thinking_event())

    state = TurnState(
        content=[
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
        ]
    )
    await lc.on_terminal_success(state)

    assert "feedback_vote:up" not in _action_ids(_last_update_blocks(fake_slack_web_client)), (
        "tool-only turn has no answer to vote on"
    )
