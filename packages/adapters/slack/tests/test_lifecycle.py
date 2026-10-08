"""Tests for SlackTurnLifecycle (lifecycle.py).

Behavioral assertions — grouped by phase:

Task 1 (debounce / registry / usage):
  - on_sse_event alone performs zero chat.postMessage and zero chat.update;
    the following on_render posts immediately (flush rides the render
    tick, not the SSE consume path).
  - Two on_render ticks within 5s trigger NO chat.update
    (debounce window).
  - Events plus an on_render tick after 5s trigger exactly one chat.update
    (debounce elapsed).
  - The first flush's registration rides post_initial(); a later
    render-tick update does not re-register.
  - _apply_usage folds usage_totals into merged usage_in / usage_out / cost_str.

on_render error propagation:
  - A failing chat.update surfaces out of on_render unswallowed -- the
    driver's per-tick render error policy is what handles it.
  - on_sse_event never raises for the identical scenario -- it performs no I/O.

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
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn

import aiohttp
import daimon.adapters.slack.lifecycle as lifecycle_module
import httpx
import pytest
import structlog
import yarl
from anthropic import BadRequestError, RateLimitError
from daimon.adapters.slack import lifecycle as lifecycle_mod
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.core.agent_identity import AgentIdentity
from daimon.core.errors import TurnError
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.stores import tenant_ledger
from daimon.core.stores.tenants import set_funding_mode
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.state import (
    McpServerFailure,
    TextBlock,
    ToolUseBlock,
    TurnState,
    UsageTotals,
)
from daimon.core.turn.termination import TerminationReason
from daimon.testing import ma_model_usage
from daimon.testing.factories import make_channel_budget, make_ledger_entry, make_tenant
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


@pytest.mark.asyncio
async def test_terminal_footer_shows_prepaid_balance_only(
    fake_slack_web_client: Any,
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
    lc, *_ = _make_lifecycle(
        fake_slack_web_client, sessionmaker=db_session_factory, tenant_id=tenant.id
    )
    await lc.post_initial()
    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="done")]))
    assert _block_text(_last_update_blocks(fake_slack_web_client)).endswith("· $12.50 left")

    async with db_session_factory() as s, s.begin():
        await set_funding_mode(s, tenant_id=tenant.id, funding_mode="operator_funded")
    lc, *_ = _make_lifecycle(
        fake_slack_web_client, sessionmaker=db_session_factory, tenant_id=tenant.id
    )
    await lc.post_initial()
    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="done")]))
    assert "$12.50 left" not in _block_text(_last_update_blocks(fake_slack_web_client))


# ---------------------------------------------------------------------------
# SSE event constructors (SimpleNamespace fakes — same pattern as Discord tests)
# ---------------------------------------------------------------------------


def _thinking_event() -> Any:
    """MA session SSE event: agent.thinking."""
    return types.SimpleNamespace(type="agent.thinking")


def _message_event(text: str) -> Any:
    """MA session SSE event: agent.message with one text part."""
    return types.SimpleNamespace(type="agent.message", content=[types.SimpleNamespace(text=text)])


def _running_tool_turn(name: str = "bash") -> TurnState:
    """Turn state with one tool call still waiting on its result."""
    call = ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name=name, input={})
    return TurnState(content=[call])


# ---------------------------------------------------------------------------
# Lifecycle factory
# ---------------------------------------------------------------------------


def _make_lifecycle(
    fake: Any,
    *,
    model_id: str = "claude-sonnet-4-6",
    agent_name: str = "test-agent",
    adopt_status_ts: str | None = None,
    header_customized: bool = False,
    notify_on_completion: bool = False,
    trigger_ts: str | None = None,
    render_tables: bool = False,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    tenant_id: uuid.UUID | None = None,
    budget_channel_id: str | None = None,
    ask_human: bool = False,
    identity: AgentIdentity | None = None,
    ma_agent_id: str | None = None,
    intent_id: uuid.UUID | None = None,
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
        ask_human=ask_human,
        notify_on_completion=notify_on_completion,
        trigger_ts=trigger_ts,
        render_tables=render_tables,
        client=fake.client,
        channel="C_TEST",
        thread_ts="1700000000.000000",
        cancel=cancel,
        author_id="U_AUTHOR",
        agent_name=agent_name,
        model_id=model_id,
        register=register,
        deregister=deregister,
        adopt_status_ts=adopt_status_ts,
        header_customized=header_customized,
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
        budget_channel_id=budget_channel_id,
        identity=identity,
        ma_agent_id=ma_agent_id,
        intent_id=intent_id,
    )
    return lc, cancel, registered, deregistered


@pytest.mark.asyncio
async def test_turn_posts_use_agent_header_and_record_intent(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, object]] = []

    async def fake_record(_sessionmaker: object, **kwargs: object) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(lifecycle_module, "record_turn_post", fake_record)
    intent = uuid.uuid4()
    tenant = uuid.uuid4()
    lc, *_ = _make_lifecycle(
        fake_slack_web_client,
        sessionmaker=db_session_factory,
        tenant_id=tenant,
        identity=AgentIdentity("Ada", "https://example.test/ada.png", False),
        ma_agent_id="agent_ada",
        intent_id=intent,
    )
    await lc.post_initial()
    await lc.post_notice("continued")
    bodies = [
        call.kwargs["json"] for call in fake_slack_web_client.mock.requests[("POST", _POST_URL)]
    ]
    assert [body["username"] for body in bodies] == ["Ada", "Ada"]
    assert [body["icon_url"] for body in bodies] == ["https://example.test/ada.png"] * 2
    assert len(recorded) == 2
    assert all(row["turn_card_intent_id"] == intent for row in recorded)
    assert all(row["channel_id"] == "C_TEST" for row in recorded)
    assert all(row["thread_ts"] == "1700000000.000000" for row in recorded)


async def test_identity_off_posts_as_bot_and_keeps_agent_footer(
    fake_slack_web_client: Any,
) -> None:
    lc, *_ = _make_lifecycle(
        fake_slack_web_client,
        agent_name="Ada",
        identity=AgentIdentity("Ada", None, True),
    )
    await lc.post_initial()
    await lc.post_notice("continued")
    posts = [
        call.kwargs["json"] for call in fake_slack_web_client.mock.requests[("POST", _POST_URL)]
    ]
    assert all("username" not in post and "icon_url" not in post for post in posts)
    assert not lc.header_customized
    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="Done.")]))
    footer = next(
        block for block in _last_update_blocks(fake_slack_web_client) if block["type"] == "context"
    )
    assert "Ada" in footer["elements"][0]["text"]


@pytest.mark.parametrize("status", [400, 429])
async def test_spend_limit_posts_notice_and_error_log(
    fake_slack_web_client: Any, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = uuid.uuid4()
    alerts: list[str] = []
    monkeypatch.setattr(
        lifecycle_module, "alert_ops", lambda url, *, key, message: alerts.append(key)
    )
    lc, *_ = _make_lifecycle(fake_slack_web_client, tenant_id=tenant_id)
    await lc.post_initial()
    body = (
        {"type": "rate_limit_error", "details": {"error_code": "enforced_spend_limit_reached"}}
        if status == 429
        else {
            "type": "invalid_request_error",
            "message": "You have reached your specified workspace API usage limits",
        }
    )
    response = httpx.Response(
        status,
        json={"type": "error", "error": body},
        request=httpx.Request("GET", "https://api.anthropic.com/v1/models"),
    )
    error = (
        RateLimitError("limit", response=response, body=body)
        if status == 429
        else BadRequestError("limit", response=response, body=body)
    )
    turn_error = TurnError(kind="upstream", cause=error)
    with structlog.testing.capture_logs() as logs:
        await lc.on_terminal_failure(TurnState(error=turn_error), turn_error)
    assert alerts == [f"spend_limit:{'org_cap' if status == 429 else 'user_limit'}"]
    assert (
        "Daimon has reached its model usage limit for now. The operators have been notified."
        in _block_text(_last_update_blocks(fake_slack_web_client))
    )
    assert {
        "event": "anthropic.spend_limit_reached",
        "log_level": "error",
        "tenant_id": str(tenant_id),
        "limit": "org_cap" if status == 429 else "user_limit",
    } in logs


# ---------------------------------------------------------------------------
# Task 1: Debounce
# ---------------------------------------------------------------------------


async def test_on_sse_event_performs_no_io_then_on_render_posts_immediately(
    fake_slack_web_client: Any,
) -> None:
    """on_sse_event alone performs zero chat-API I/O; a following on_render posts.

    The flush moved off the SSE consume path: folding an event is a
    local reducer call only, and the first chat.postMessage now happens on
    the next render tick.
    """
    lc, *_ = _make_lifecycle(fake_slack_web_client)

    await lc.on_sse_event(_thinking_event())

    assert _post_count(fake_slack_web_client) == 0, (
        "on_sse_event must perform zero chat.postMessage — the flush rides the render tick"
    )
    assert _update_count(fake_slack_web_client) == 0, (
        "on_sse_event must perform zero chat.update — no I/O on the SSE consume path"
    )

    await lc.on_render(TurnState())

    assert _post_count(fake_slack_web_client) == 1, (
        "the following render tick must trigger exactly one chat.postMessage (immediate flush)"
    )
    assert _update_count(fake_slack_web_client) == 0, (
        "no chat.update on the first flush — status message not yet established"
    )


async def test_second_event_within_debounce_no_update(fake_slack_web_client: Any) -> None:
    """Two render ticks inside the debounce window produce one post, no update."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)

    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())  # first render tick — immediate post, no debounce
    await lc.on_render(_running_tool_turn())  # second render tick — still within debounce

    assert _post_count(fake_slack_web_client) == 1, (
        "second render tick within debounce must not post a new message"
    )
    assert _update_count(fake_slack_web_client) == 0, "no chat.update within the 5s debounce window"


async def test_event_after_debounce_triggers_update(fake_slack_web_client: Any) -> None:
    """A render tick with a new tool call past the debounce window triggers one chat.update."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)

    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())  # initial post
    # Backdate _last_flush to simulate 6s elapsed (established idiom from Discord tests)
    lc._last_flush = time.monotonic() - 6.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce

    await lc.on_render(_running_tool_turn())

    assert _post_count(fake_slack_web_client) == 1, "debounced update must NOT post a new message"
    assert _update_count(fake_slack_web_client) == 1, (
        "exactly one chat.update after debounce window elapses"
    )


# ---------------------------------------------------------------------------
# Task 1: Registry
# ---------------------------------------------------------------------------


async def test_first_flush_registers_status_ts(fake_slack_web_client: Any) -> None:
    """The first flush's registration rides post_initial(); a later
    render-tick update must not re-register."""
    lc, cancel, registered, _ = _make_lifecycle(fake_slack_web_client)

    await lc.post_initial()

    assert len(registered) == 1, "exactly one registration after post_initial()'s first flush"
    ts, (reg_event, reg_author) = next(iter(registered.items()))
    assert ts == "1000000000.000001", (
        "registered ts must match the ts from the chat.postMessage response"
    )
    assert reg_event is cancel, "registered cancel event must be the one injected at construction"
    assert reg_author == "U_AUTHOR", "registered author_id must match the constructor arg"

    # A later render-tick update (debounce elapsed) must not add a second registration.
    await lc.on_sse_event(_thinking_event())
    lc._last_flush = time.monotonic() - 6.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce
    await lc.on_render(TurnState())

    assert len(registered) == 1, "a debounced render-tick update must not add a second registration"


# ---------------------------------------------------------------------------
# Adopting construction path (dead-session recovery): the caller hands over
# the pre-recovery card's ts instead of letting the lifecycle post a new one.
# ---------------------------------------------------------------------------

_ADOPTED_TS = "1111.1"


async def test_status_ts_reports_the_adopted_ts_before_anything_is_posted(
    fake_slack_web_client: Any,
) -> None:
    """An adopting lifecycle reports the adopted ts with no chat-API I/O.

    The caller can read status_ts immediately after construction, before
    the turn has rendered anything at all.
    """
    lc, *_ = _make_lifecycle(fake_slack_web_client, adopt_status_ts=_ADOPTED_TS)

    assert lc.status_ts == _ADOPTED_TS, "status_ts must report the adopted ts immediately"
    assert _post_count(fake_slack_web_client) == 0, (
        "reading status_ts on an adopting lifecycle must not perform any chat-API I/O"
    )


async def test_adopted_agent_header_stays_out_of_footer(fake_slack_web_client: Any) -> None:
    lc, *_ = _make_lifecycle(
        fake_slack_web_client,
        adopt_status_ts=_ADOPTED_TS,
        agent_name="Ada",
        header_customized=True,
    )
    assert lc.header_customized
    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="Done.")]))
    blocks = _last_update_blocks(fake_slack_web_client)
    footer = next(block for block in blocks if block["type"] == "context")
    assert "Ada" not in footer["elements"][0]["text"]
    assert _post_count(fake_slack_web_client) == 0


async def test_an_adopting_lifecycle_updates_the_adopted_card_instead_of_posting_a_new_one(
    fake_slack_web_client: Any,
) -> None:
    """An adopting lifecycle's first flush updates the adopted card, not a new one.

    A recovered turn must not leave a second card behind: nothing would ever
    finalise it, and the marker written against the pre-recovery mapping row
    would address a card nobody is rendering into.
    """
    lc, *_ = _make_lifecycle(fake_slack_web_client, adopt_status_ts=_ADOPTED_TS)

    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())

    assert _post_count(fake_slack_web_client) == 0, (
        "a recovered turn must not post a second status card"
    )
    assert _update_count(fake_slack_web_client) == 1, (
        "an adopting lifecycle's first render tick must update, not post"
    )
    calls = fake_slack_web_client.mock.requests.get(("POST", _UPDATE_URL), [])
    body = calls[-1].kwargs["json"]
    assert body["ts"] == _ADOPTED_TS, "the update must target the adopted ts"
    assert body["channel"] == "C_TEST", "the update must target the constructor's channel"


async def test_an_adopting_lifecycles_first_render_tick_updates_without_waiting_out_the_debounce(
    fake_slack_web_client: Any,
) -> None:
    """An adopting lifecycle's first render tick updates immediately, no debounce wait.

    The debounce clock starts already satisfied against a monotonic clock, so
    the very first flush after construction updates with no wait -- this test
    pins that consequence with no debounce-window backdating of its own. If a
    future change seeds the debounce clock from `time.monotonic()` at
    construction instead of leaving it at its zero value, this goes red.
    """
    lc, *_ = _make_lifecycle(fake_slack_web_client, adopt_status_ts=_ADOPTED_TS)

    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())

    assert _update_count(fake_slack_web_client) == 1, (
        "the first render tick on an adopting lifecycle must update immediately, "
        "with no debounce wait -- if a future change seeds the debounce clock from "
        "time.monotonic() at construction, this must fail"
    )


async def test_an_adopting_lifecycle_registers_no_cancel_entry_of_its_own(
    fake_slack_web_client: Any,
) -> None:
    """An adopting lifecycle never registers a cancel entry of its own.

    It never takes the first-post branch that registers -- the caller
    handing over the ts owns rebinding the registry entry.
    """
    lc, _, registered, _ = _make_lifecycle(fake_slack_web_client, adopt_status_ts=_ADOPTED_TS)

    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())

    assert registered == {}, (
        "an adopting lifecycle must not register anything -- the caller rebinds"
    )


async def test_a_terminal_success_on_an_adopting_lifecycle_replaces_the_adopted_card(
    fake_slack_web_client: Any,
) -> None:
    """The terminal render on an adopting lifecycle lands on the adopted card.

    This is the assertion that the answer ends up on the card the user has
    been watching, not on a second, freshly-posted one.
    """
    lc, _, _registered, deregistered = _make_lifecycle(
        fake_slack_web_client, adopt_status_ts=_ADOPTED_TS
    )

    state = TurnState(content=[TextBlock(kind="text", text="The answer is 42.")])
    await lc.on_terminal_success(state)

    assert _post_count(fake_slack_web_client) == 0, "a single chunk needs no overflow post"
    calls = fake_slack_web_client.mock.requests.get(("POST", _UPDATE_URL), [])
    assert calls, "terminal success on an adopting lifecycle must chat.update"
    body = calls[-1].kwargs["json"]
    assert body["ts"] == _ADOPTED_TS, "the terminal render must target the adopted ts"
    assert "The answer is 42." in _block_text(body["blocks"]), (
        "the answer must land on the adopted card, the one the user has been watching"
    )
    assert lc.final_ts == _ADOPTED_TS, "final_ts must equal the adopted ts"
    assert deregistered == [_ADOPTED_TS], "the adopted ts must be deregistered on terminal success"


# ---------------------------------------------------------------------------
# on_render must not swallow adapter failures -- the driver's per-tick
# render error policy is what handles them.
# ---------------------------------------------------------------------------


async def test_raising_chat_update_propagates_out_of_on_render(
    fake_slack_web_client: Any,
) -> None:
    """A rate-limited/failing chat.update surfaces out of on_render -- the
    adapter does not swallow it, so the driver's per-tick policy handles it."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)

    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())  # first post — no update yet, succeeds
    lc._last_flush = time.monotonic() - 6.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce

    _reset_slack_responses(fake_slack_web_client)
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_UPDATE_URL),
        payload={"ok": False, "error": "ratelimited"},
    )

    with pytest.raises(SlackApiError):
        await lc.on_render(_running_tool_turn())


async def test_on_sse_event_never_raises_for_the_same_scenario(
    fake_slack_web_client: Any,
) -> None:
    """The cheap local tap performs no I/O, so a failing chat.update never
    reaches it -- only the render tick can hit that failure."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)

    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())
    lc._last_flush = time.monotonic() - 6.0  # pyright: ignore[reportPrivateUsage]  # backdating debounce

    _reset_slack_responses(fake_slack_web_client)
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        str(_UPDATE_URL),
        payload={"ok": False, "error": "ratelimited"},
    )

    # Does not raise even though the next render tick would hit the failing
    # update -- on_sse_event performs no I/O at all.
    await lc.on_sse_event(_message_event("Checking the logs"))
    assert lc._state.text_preview == "Checking the logs", (  # pyright: ignore[reportPrivateUsage]
        "the tap still folded the event into the card"
    )


# ---------------------------------------------------------------------------
# post_initial() / status_ts
# ---------------------------------------------------------------------------


async def test_post_initial_posts_the_card_and_registers_cancel_before_any_sse_event(
    fake_slack_web_client: Any,
) -> None:
    """post_initial() alone posts the card and registers Cancel, with no prior SSE event."""
    lc, cancel, registered, _ = _make_lifecycle(fake_slack_web_client)

    await lc.post_initial()

    assert _post_count(fake_slack_web_client) == 1, (
        "post_initial() must post exactly one chat.postMessage with no SSE event required"
    )
    assert lc.status_ts == CHAT_OK_PAYLOAD["ts"], (
        "status_ts must reflect the ts returned by the post"
    )
    assert len(registered) == 1, (
        "the Cancel button must exist before the first SSE event, not after it"
    )
    ts, (reg_event, reg_author) = next(iter(registered.items()))
    assert ts == lc.status_ts, "registered ts must match status_ts"
    assert reg_event is cancel, "registered cancel event must be the one injected at construction"
    assert reg_author == "U_AUTHOR", "registered author_id must match the constructor arg"


async def test_without_intent_id_cancel_value_still_uses_status_timestamp(
    fake_slack_web_client: Any,
) -> None:
    """Omitting intent_id keeps the legacy status-ts value on later card edits."""
    clock_now = [100.0]
    lc = SlackTurnLifecycle(
        client=fake_slack_web_client.client,
        channel="C_TEST",
        thread_ts="1700000000.000000",
        cancel=asyncio.Event(),
        author_id="U_AUTHOR",
        agent_name="test-agent",
        model_id="claude-sonnet-4-6",
        register=lambda ts, event, author: None,
        deregister=lambda ts: None,
        clock=lambda: clock_now[0],
    )

    await lc.post_initial()
    clock_now[0] += 6.0
    await lc.on_render(TurnState())

    update_blocks = _last_update_blocks(fake_slack_web_client)
    cancel_button = next(
        element
        for block in update_blocks
        if block.get("type") == "actions"
        for element in block.get("elements", [])
    )
    assert cancel_button["value"] == lc.status_ts


async def test_status_ts_is_none_before_anything_is_posted(fake_slack_web_client: Any) -> None:
    """A freshly constructed lifecycle reports status_ts as None."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)

    assert lc.status_ts is None, "status_ts must be None before anything has been posted"


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
            ma_model_usage(
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
    await lc.post_initial()  # initial post (1 postMessage)
    await lc.on_sse_event(_thinking_event())  # folds the thinking trail entry, no flush

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
    await lc.post_initial()
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
    await lc.post_initial()
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


@pytest.mark.parametrize("partial_text", ["", "Partial analysis before cancellation."])
async def test_interrupted_tool_turn_shows_cancelled_and_preserves_partial_answer(
    fake_slack_web_client: Any, partial_text: str
) -> None:
    lc, _, _, deregistered = _make_lifecycle(fake_slack_web_client, notify_on_completion=True)
    await lc.post_initial()
    content = [
        ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={})
    ]
    if partial_text:
        content.append(TextBlock(kind="text", text=partial_text))

    await lc.on_terminal_success(
        TurnState(content=content, termination=TerminationReason.INTERRUPTED)
    )

    rendered = _block_text(_last_update_blocks(fake_slack_web_client))
    assert "Stopped." in rendered
    if partial_text:
        assert partial_text in rendered
    assert "cancel_turn" not in _action_ids(_last_update_blocks(fake_slack_web_client))
    assert _post_count(fake_slack_web_client) == 1, "cancellation must not send a completion ping"
    assert lc.final_ts == lc.status_ts
    assert lc.status_ts in deregistered


async def test_terminal_success_tool_only_leaves_collapsed_done(
    fake_slack_web_client: Any,
) -> None:
    """Tool-only turn (no final text after last ToolUseBlock) leaves the collapsed done; no overflow."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
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
    assert "cancel_turn" not in _action_ids(blocks), (
        "tool-only terminal must collapse to the done footer with no cancel button"
    )
    assert any(b["type"] == "context" for b in blocks), (
        "tool-only terminal must render the done footer context block"
    )
    assert not any(b.get("type") == "markdown" for b in blocks), (
        "tool-only turn has no final answer — no markdown answer block"
    )


@pytest.mark.parametrize("final_text", ["", "The follow-up is complete."])
async def test_terminal_success_keeps_substantive_answer_before_trailing_tool(
    fake_slack_web_client: Any, final_text: str
) -> None:
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    sealed_answer = "The analysis is complete. " + "x" * 500
    content = [
        TextBlock(kind="text", text=sealed_answer),
        ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="memory", input={}),
    ]
    if final_text:
        content.append(TextBlock(kind="text", text=final_text))

    await lc.on_terminal_success(TurnState(content=content))

    rendered = _block_text(_last_update_blocks(fake_slack_web_client))
    assert sealed_answer in rendered, "a later tool must not erase an already completed answer"
    if final_text:
        assert rendered.index(sealed_answer) < rendered.index(final_text), (
            "a final recap must follow the sealed answer"
        )
    assert lc.final_ts is not None, "a delivered sealed answer needs a final watermark"


async def test_terminal_success_empty_content_shows_turn_cancelled(
    fake_slack_web_client: Any,
) -> None:
    """Empty content (no blocks) triggers a chat.update with 'Turn cancelled.'."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    state = TurnState()  # completely empty
    await lc.on_terminal_success(state)

    assert _update_count(fake_slack_web_client) >= 1, (
        "cancelled turn must trigger at least one chat.update"
    )
    assert lc.final_ts is not None, "final_ts must be set even for cancelled turns"

    blocks = _last_update_blocks(fake_slack_web_client)
    assert "Stopped.\nSend a message to start again." in _block_text(blocks), (
        "an empty turn shows the stop and next step"
    )
    assert not _has_actions_block(blocks), "cancelled turn must not keep the cancel button"


# ---------------------------------------------------------------------------
# Task 2: Terminal — failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        TerminationReason.CONNECTION_LOST,
        TerminationReason.UPSTREAM,
        TerminationReason.INTERRUPTED,
        TerminationReason.INTERRUPT_TIMEOUT,
        TerminationReason.REQUIRES_ACTION,
        TerminationReason.CEILING,
        TerminationReason.MCP_DEGRADED_EMPTY,
    ],
    ids=str,
)
async def test_terminal_failure_card_carries_the_termination_notice(
    fake_slack_web_client: Any, reason: TerminationReason
) -> None:
    """The error card explains the reason: a notice section above the ❌ summary."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    state = TurnState(
        termination=reason,
        content=[
            ToolUseBlock(
                kind="tool_use", id="tu_1", type="agent.tool_use", name="fit_model", input={}
            )
        ],
    )

    await lc.on_terminal_failure(state, RuntimeError("x" * 300))

    blocks = _last_update_blocks(fake_slack_web_client)
    notice = render_termination_notice(reason, state=state)
    assert notice is not None
    section, context = blocks[2], blocks[-1]
    assert section["type"] == "section"
    assert notice.cause in section["text"]["text"]
    assert notice.next_step in section["text"]["text"], "the next step is not truncated away"
    assert "`fit_model`" in section["text"]["text"], "work in flight is named"
    assert "`rid: " in section["text"]["text"]
    assert "0s" in context["elements"][0]["text"]
    assert "xxx" not in _block_text(blocks), "raw error stays in the logs"


async def test_terminal_failure_notice_fits_slack_limits_with_many_long_names(
    fake_slack_web_client: Any,
) -> None:
    """45 failed servers and 45 running tools, every name 100 characters: the
    section stays under Slack's 3,000-character limit and the top-level text
    carries the notice instead of a bare phase name."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
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

    await lc.on_terminal_failure(state, RuntimeError("x"))

    calls = fake_slack_web_client.mock.requests.get(("POST", _UPDATE_URL), [])
    body = calls[-1].kwargs["json"]
    section = body["blocks"][2]["text"]["text"]
    assert len(section) <= 3000
    assert "and 42 more" in section and "and 40 more" in section
    assert "`rid: " in section
    assert body["text"].startswith("Tool connection failed: "), "fallback text is the notice"
    assert len(body["text"]) <= 3000


async def test_a_notice_that_fails_to_build_still_draws_the_error_card(
    fake_slack_web_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("renderer broke")

    monkeypatch.setattr(lifecycle_module, "render_termination_notice", _broken)
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()

    await lc.on_terminal_failure(TurnState(), RuntimeError("upstream timeout"))

    blocks = _last_update_blocks(fake_slack_web_client)
    assert blocks[0]["text"]["text"] == "Something went wrong."
    assert not any("upstream timeout" in str(block) for block in blocks)


async def test_the_card_reuses_the_rid_bound_for_the_turn(fake_slack_web_client: Any) -> None:
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()

    with structlog.contextvars.bound_contextvars(rid="01BOUNDRID"):
        await lc.on_terminal_failure(TurnState(), RuntimeError("x"))

    assert "`rid: 01BOUNDRID`" in _block_text(_last_update_blocks(fake_slack_web_client))


async def test_terminal_failure_does_not_raise(fake_slack_web_client: Any) -> None:
    """on_terminal_failure updates/posts error state and does NOT raise."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    state = TurnState()
    err = RuntimeError("upstream blew up")

    # Must not raise — lifecycle boundary never re-raises
    await lc.on_terminal_failure(state, err)

    blocks = _last_update_blocks(fake_slack_web_client)
    text = _block_text(blocks)
    assert "Something went wrong." in text
    assert "`rid: " in text, "the notice carries a request id to find the logged error"
    assert "upstream blew up" not in text, "raw exception text stays in the logs"
    assert not _has_actions_block(blocks), "error terminal must drop the cancel button"


# ---------------------------------------------------------------------------
# Task 2: Terminal — registry deregister in finally
# ---------------------------------------------------------------------------


async def test_deregister_called_in_terminal_success_finally(
    fake_slack_web_client: Any,
) -> None:
    """deregister is invoked for status_ts in the on_terminal_success finally block."""
    lc, _, _registered, deregistered = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
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
    await lc.post_initial()
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
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    state = TurnState(content=[TextBlock(kind="text", text="The answer is 42.")])
    await lc.on_terminal_success(state)  # must not raise

    assert _update_count(fake_slack_web_client) == 2, (
        "the failed answer replace must be followed by exactly one repair chat.update"
    )
    blocks = _last_update_blocks(fake_slack_web_client)
    assert "I couldn't send the full answer." in _block_text(blocks), (
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
    await lc.post_initial()
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
    await lc.post_initial()
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
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    await lc.on_terminal_failure(TurnState(), RuntimeError("upstream blew up"))

    assert _update_count(fake_slack_web_client) == 2, (
        "the failed error flush must be followed by exactly one repair chat.update"
    )
    blocks = _last_update_blocks(fake_slack_web_client)
    assert "Something went wrong." in _block_text(blocks), (
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
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    state = TurnState(content=[TextBlock(kind="text", text="Hello.")])
    await lc.on_terminal_success(state)  # must not raise

    blocks = _last_update_blocks(fake_slack_web_client)
    assert "I couldn't send the full answer." in _block_text(blocks), (
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
    await lc.post_initial()
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
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    await lc.on_terminal_success(TurnState())  # empty content — cancelled path

    text = _block_text(_last_update_blocks(fake_slack_web_client))
    assert "Something went wrong." in text
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
    await lc.post_initial()
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
    await lc.post_initial()
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
    await lc.post_initial()
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


async def test_ask_human_button_rides_with_the_vote_buttons_when_enabled(
    fake_slack_web_client: Any,
) -> None:
    lc, *_ = _make_lifecycle(fake_slack_web_client, ask_human=True)
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="42.")]))

    ids = _action_ids(_last_update_blocks(fake_slack_web_client))
    assert ids[-3:] == ["feedback_vote:up", "feedback_vote:down", "support_escalate"], (
        "Ask a human sits after the two votes on the final answer"
    )


async def test_no_ask_human_button_when_support_is_off(fake_slack_web_client: Any) -> None:
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="42.")]))

    assert "support_escalate" not in _action_ids(_last_update_blocks(fake_slack_web_client)), (
        "an unset Slack escalation channel must not render the button"
    )


async def test_tool_only_turn_gets_feedback_buttons_on_its_card(fake_slack_web_client: Any) -> None:
    """A tool-only turn's product is its work (a file, a chart), so its card is
    votable, as a tool-only Discord turn is."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    state = TurnState(
        content=[
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
        ]
    )
    await lc.on_terminal_success(state)

    ids = _action_ids(_last_update_blocks(fake_slack_web_client))
    assert "feedback_vote:up" in ids and "feedback_vote:down" in ids
    assert "cancel_turn" not in ids


async def test_tool_only_card_offers_ask_a_human_when_enabled(fake_slack_web_client: Any) -> None:
    lc, *_ = _make_lifecycle(fake_slack_web_client, ask_human=True)
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    state = TurnState(
        content=[
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
        ]
    )
    await lc.on_terminal_success(state)

    assert "support_escalate" in _action_ids(_last_update_blocks(fake_slack_web_client))


async def test_cancelled_turn_gets_no_feedback_buttons(fake_slack_web_client: Any) -> None:
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    await lc.on_terminal_success(TurnState(termination=TerminationReason.INTERRUPTED))

    assert "feedback_vote:up" not in _action_ids(_last_update_blocks(fake_slack_web_client))


async def test_terminal_success_names_the_failed_mcp_server_under_the_reply(
    fake_slack_web_client: Any,
) -> None:
    """#79: the reply is posted with the dropped server named under it."""
    from daimon.core.turn.state import McpServerFailure

    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
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

    blocks = _last_update_blocks(fake_slack_web_client)
    assert blocks[0]["text"].startswith("Here is the board."), "the reply itself comes first"
    assert "notion" in blocks[0]["text"], "the dropped server is named under the reply"


async def test_tool_only_turn_posts_the_failed_mcp_server_notice_on_its_own(
    fake_slack_web_client: Any,
) -> None:
    """#79: no reply to hang the notice under, so it is posted as its own message."""
    from daimon.core.turn.state import McpServerFailure

    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())
    initial_posts = _post_count(fake_slack_web_client)

    state = TurnState(
        content=[
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
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

    post_calls = fake_slack_web_client.mock.requests.get(("POST", _POST_URL), [])
    assert len(post_calls) == initial_posts + 1, "exactly one notice is posted"
    assert "notion" in post_calls[-1].kwargs["json"]["text"], "the dropped server is named"


async def test_completion_ping_is_fresh_and_only_mentions_requester(fake_slack_web_client):
    fake = fake_slack_web_client
    lifecycle, _, _, _ = _make_lifecycle(fake, notify_on_completion=True)
    await lifecycle.post_initial()
    await lifecycle.on_terminal_success(
        TurnState(content=[TextBlock(kind="text", text="Done <@U_OTHER> <!channel>")])
    )
    calls = fake.mock.requests[("POST", _POST_URL)]
    assert len(calls) == 2
    body = calls[-1].kwargs["json"]
    assert "&lt;@U_OTHER&gt;" in body["blocks"][0]["text"]
    assert "&lt;!channel&gt;" in body["blocks"][0]["text"]
    assert body["blocks"][0]["text"].startswith("<@")
    assert lifecycle.final_ts is not None
    assert await lifecycle.prepend_revealed_answer("Recovered files.")
    assert _last_update_blocks(fake)[0]["text"].startswith("Recovered files.")


@pytest.mark.parametrize("enabled", [False, True])
async def test_reactions_target_trigger_not_thread_root(fake_slack_web_client, enabled):
    import re

    fake = fake_slack_web_client
    fake.mock.post(re.compile(r"https://slack\.com/api/reactions\.remove.*"), payload={"ok": True})
    lifecycle, _, _, _ = _make_lifecycle(
        fake, trigger_ts="1700000000.000999", notify_on_completion=enabled
    )
    # The app adds eyes at admission, before constructing the lifecycle.
    await fake.client.reactions_add(channel="C_TEST", timestamp="1700000000.000999", name="eyes")
    await lifecycle.on_acknowledgment("accepted")
    await lifecycle.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="done")]))
    await lifecycle.on_acknowledgment("done")
    urls = [str(url) for method, url in fake.mock.requests if "reactions." in str(url)]
    assert len(urls) == (3 if enabled else 1)
    assert all("timestamp=1700000000.000999" in url for url in urls)
    assert any("name=eyes" in url and "reactions.add" in url for url in urls)
    assert any("name=white_check_mark" in url for url in urls) is enabled
    assert any("reactions.remove" in url for url in urls) is enabled


@pytest.mark.parametrize("notify", [False, True])
async def test_native_table_survives_late_continuity_notice(fake_slack_web_client, notify):
    fake = fake_slack_web_client
    lifecycle, _, _, _ = _make_lifecycle(fake, render_tables=True, notify_on_completion=notify)
    await lifecycle.post_initial()
    await lifecycle.on_terminal_success(
        TurnState(
            content=[
                TextBlock(kind="text", text="| Name | Value |\n| --- | ---: |\n| Example | 42 |")
            ]
        )
    )
    original_blocks = (
        fake.mock.requests[("POST", _POST_URL)][-1].kwargs["json"]["blocks"]
        if notify
        else _last_update_blocks(fake)
    )
    assert sum(block["type"] == "table" for block in original_blocks) == 1
    assert lifecycle.final_ts is not None
    assert await lifecycle.prepend_revealed_answer("Recovered files.")
    blocks = _last_update_blocks(fake)
    assert blocks[0]["type"] == "markdown"
    table_blocks = [block for block in blocks if block["type"] == "table"]
    assert len(table_blocks) == 1 and table_blocks[0]["rows"][1][1]["text"] == "42"


@pytest.mark.parametrize("prefix", ["", "Before\n\n"])
@pytest.mark.parametrize("notify", [False, True])
async def test_rejected_native_table_retries_plain_chunks_without_duplicate_prose(
    fake_slack_web_client, prefix, notify
):
    from aioresponses import CallbackResult
    from structlog.testing import capture_logs

    fake = fake_slack_web_client
    lifecycle, _, _, deregistered = _make_lifecycle(
        fake, render_tables=True, notify_on_completion=notify
    )
    await lifecycle.post_initial()
    _reset_slack_responses(fake)
    delivered = []
    rejected = []

    def respond(url, **kwargs):
        body = kwargs["json"]
        if any(block.get("type") == "table" for block in body["blocks"]):
            rejected.append(body)
            return CallbackResult(payload={"ok": False, "error": "invalid_blocks"})
        delivered.append(body)
        return CallbackResult(
            payload={"ok": True, "channel": "C_TEST", "ts": f"1700000000.00099{len(delivered)}"}
        )

    fake.mock.post(str(_UPDATE_URL), callback=respond, repeat=True)
    fake.mock.post(str(_POST_URL), callback=respond, repeat=True)
    table = "| " + " | ".join(f"C{i}" for i in range(20)) + " |\n"
    table += "| " + " | ".join(["---"] * 20) + " |\n"
    table += "\n".join("| " + " | ".join([f"{i:04}"] * 20) + " |" for i in range(99))
    assert len(table) > 11800  # Cell data fits the budget; Markdown syntax needs splitting.
    with capture_logs() as logs:
        await lifecycle.on_terminal_success(
            TurnState(content=[TextBlock(kind="text", text=prefix + table + "\n\nAfter <@U999>")])
        )
    markdown = [
        block["text"]
        for body in delivered
        for block in body["blocks"]
        if block.get("type") == "markdown"
    ]
    answer = "\n".join(markdown)
    assert len(rejected) == 1
    assert "| C0 | C1 |" in answer
    assert all(answer.count(f"{i:04}") == 20 for i in range(99))
    assert answer.count("Before") == (1 if prefix else 0)
    assert answer.count("After") == 1
    assert answer.count("<@U_AUTHOR>") == (1 if notify else 0)
    assert ("<@U999>" not in answer) is notify
    assert all(len(chunk) <= 11800 for chunk in markdown)
    assert lifecycle.final_ts == f"1700000000.00099{len(delivered)}"
    assert deregistered
    assert (
        sum(block.get("type") == "actions" for body in delivered for block in body["blocks"]) == 1
    )
    assert any(entry["event"] == "turn.table_delivery_failed" for entry in logs)


async def test_terminal_success_linkifies_emphasized_urls(fake_slack_web_client: Any) -> None:
    """A final answer wrapping a bare URL in ** must be normalized to an explicit
    markdown link before posting, so Slack's autolinker cannot absorb the closing
    asterisks into the URL (reported: notebook URL rendered with a trailing '*')."""
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())

    state = TurnState(
        content=[TextBlock(kind="text", text="Here: **🔗 https://x.up.railway.app/n/abc**")]
    )
    await lc.on_terminal_success(state)

    blocks = _last_update_blocks(fake_slack_web_client)
    assert blocks[0]["type"] == "markdown"
    assert (
        "[https://x.up.railway.app/n/abc](https://x.up.railway.app/n/abc)" in blocks[0]["text"]
    ), "the emphasized bare URL must be rewritten to an explicit [url](url) link"


@pytest.mark.asyncio
async def test_terminal_footer_shows_an_active_channel_budgets_remainder(
    fake_slack_web_client: Any,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("12.50"))
    await make_channel_budget(db_session, tenant=tenant, channel_id="C1", limit_usd=Decimal("5"))
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("-1.25"), channel_id="C1")
    await make_channel_budget(
        db_session,
        tenant=tenant,
        channel_id="C2",
        window="total",
        starts_at=datetime.now(UTC) + timedelta(days=1),
    )
    await db_session.commit()

    footers: dict[str | None, str] = {}
    for channel in ("C1", "C2", "C3", None):
        lc, *_ = _make_lifecycle(
            fake_slack_web_client,
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            budget_channel_id=channel,
        )
        await lc.post_initial()
        await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="done")]))
        footers[channel] = _block_text(_last_update_blocks(fake_slack_web_client))
    assert footers["C1"].endswith("· $3.75 of channel budget left"), "the budget's remainder"
    for channel in ("C2", "C3", None):
        assert footers[channel].endswith("· $11.25 left"), (
            f"{channel}: an inactive or missing budget shows the tenant balance"
        )


async def test_code_is_raw_in_the_block_and_escaped_in_the_notification_text(
    fake_slack_web_client: Any,
) -> None:
    lc, *_ = _make_lifecycle(fake_slack_web_client)
    await lc.post_initial()
    await lc.on_terminal_success(
        TurnState(content=[TextBlock(kind="text", text="Try `<!channel> <@U2> a<b`")])
    )
    body = fake_slack_web_client.mock.requests[("POST", _UPDATE_URL)][-1].kwargs["json"]
    assert body["blocks"][0]["text"] == "Try `<!channel> <@U2> a<b`", (
        "the markdown block shows code verbatim"
    )
    assert body["text"] == "Try `&lt;!channel&gt; &lt;@U2&gt; a&lt;b`", (
        "the mrkdwn fallback parses code too, so nothing in it may ping"
    )
