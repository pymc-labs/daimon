"""Tests for DiscordTurnLifecycle — embed state machine, debounce, clean replace.

Uses plain async recorder functions. No AsyncMock, no MagicMock,
no FakeMessage for lifecycle send/edit mocks. The edit callable receives the
message reference as its first positional argument.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import types
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn
from unittest.mock import MagicMock

import daimon.adapters.discord.lifecycle as lifecycle_module
import discord
import httpx
import pytest
import structlog
from anthropic import BadRequestError, RateLimitError
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.theme import COLOR_RED
from daimon.core.errors import TurnError, UserFacingError
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.stores import tenant_ledger
from daimon.core.stores.tenants import set_funding_mode
from daimon.core.tenant_balance import debit_amount
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import McpServerFailure, TextBlock, ToolUseBlock, TurnState
from daimon.core.turn.status_lines import SUMMARY_GAP as GAP
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_channel_budget, make_ledger_entry, make_tenant
from discord.http import HTTPClient, Route
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL_REF = object()  # opaque message reference


@pytest.mark.parametrize("replacement_source", ["missing", "transport"])
async def test_terminal_waits_for_missing_card_replacement_across_handover(
    replacement_source: str,
) -> None:
    """A replacement send cannot appear after the recovered answer."""
    send_started, release_send = asyncio.Event(), asyncio.Event()
    cards: dict[int, dict[str, Any]] = {}
    refs: list[Any] = []

    async def send(**kwargs: Any) -> Any:
        ref = MagicMock(spec=discord.Message)
        ref.id = 1000 + len(refs)
        refs.append(ref)
        if ref.id == 1001:
            send_started.set()
            await release_send.wait()
        cards[ref.id] = dict(kwargs)
        return ref

    async def edit(ref: Any, **kwargs: Any) -> Any:
        if replacement_source == "transport" and ref.id == 1000 and ref.id not in cards:
            return await send(**kwargs)
        if ref.id not in cards:
            raise discord.NotFound(
                types.SimpleNamespace(status=404, reason="gone"),
                {"code": 10008, "message": "gone"},
            )
        cards[ref.id].update(kwargs)
        return ref

    async def delete(ref: Any) -> None:
        cards.pop(ref.id, None)

    old = DiscordTurnLifecycle(
        send=send, edit=edit, delete=delete, agent_name="test", model_id="m", cancel_view="STOP"
    )
    await old.post_initial()
    del cards[1000]
    old._last_flush = -100
    tick = asyncio.create_task(old.on_render(_running_tool_turn()))
    await asyncio.wait_for(send_started.wait(), 1)
    tick.cancel()
    await asyncio.gather(tick, return_exceptions=True)
    pending = set(old._progress_edits)
    finish = asyncio.create_task(old.on_terminal_failure(TurnState(), RuntimeError("retry")))
    await asyncio.sleep(0.02)
    assert not finish.done(), "the failure card must wait for the replacement send"
    release_send.set()
    await asyncio.wait_for(asyncio.gather(*pending), 1)
    await asyncio.wait_for(finish, 1)
    successor = DiscordTurnLifecycle(
        send=send,
        edit=edit,
        delete=delete,
        agent_name="test",
        model_id="m",
        adopt_message_ref=old.release_message_ref(),
        adopt_pending_progress=old,
    )
    await successor.on_terminal_success(_make_success_state("Recovered answer"))
    assert len(cards) == 1
    card = next(iter(cards.values()))
    assert card["content"] == "Recovered answer"
    assert card["view"] is None


@pytest.mark.parametrize("extra_render", [False, True])
@pytest.mark.parametrize(
    "ending", ["answer", "long_answer", "stopped", "failure", "external", "recovered"]
)
async def test_late_progress_edits_cannot_overwrite_terminal_delivery(
    monkeypatch: pytest.MonkeyPatch, extra_render: bool, ending: str
) -> None:
    """A held progress request cannot delay delivery; a late result is repaired."""
    monkeypatch.setattr(lifecycle_module, "_PROGRESS_SETTLE_S", 0.01)
    releases = [asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event()]
    current: dict[str, Any] = {}
    sends: list[dict[str, Any]] = []
    progress_count = 0
    clock = [0.0]

    async def send(**kwargs: Any) -> object:
        sends.append(kwargs)
        if len(sends) == 1:
            current.update(kwargs)
        return _SENTINEL_REF

    async def edit(ref: Any, **kwargs: Any) -> None:
        nonlocal progress_count
        embeds = kwargs.get("embeds", [])
        if embeds and embeds[0].title and "Working" in embeds[0].title:
            index = progress_count
            progress_count += 1
            started[index].set()
            await releases[index].wait()
        current.update(kwargs)

    lc = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="test", model_id="m", clock=lambda: clock[0]
    )
    await lc.post_initial()
    clock[0] = 30.0
    tick = asyncio.create_task(lc.on_render(_running_tool_turn()))
    await asyncio.wait_for(started[0].wait(), 1)
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    if extra_render:
        # A second render queues behind the first edit.
        tick = asyncio.create_task(lc.on_render(_running_tool_turn()))
        await asyncio.sleep(0)
        assert progress_count == 1
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick
    pending = set(lc._progress_edits)
    lc.on_render_stopped()  # the driver does this before cancelling its loop
    text = "x" * 4000 if ending == "long_answer" else "Final answer"
    state = _make_success_state(text)
    if ending == "stopped":
        state = TurnState(termination=TerminationReason.INTERRUPTED)
    await lc.on_render(state)  # guarded final render must not start another edit
    assert progress_count == 1
    if ending in {"failure", "recovered"}:
        finish = asyncio.create_task(lc.on_terminal_failure(state, RuntimeError("failed")))
    elif ending == "external":
        finish = asyncio.create_task(lc.end_card("Outer turn failure"))
    else:
        finish = asyncio.create_task(lc.on_terminal_success(state))
    await asyncio.wait_for(finish, 1)
    delivered = dict(current)
    assert delivered.get("view") is None
    assert not releases[0].is_set(), "terminal delivery must precede held progress"
    releases[0].set()
    await asyncio.wait_for(asyncio.gather(*pending), 1)
    if ending == "recovered":
        successor = DiscordTurnLifecycle(
            send=send,
            edit=edit,
            agent_name="test",
            model_id="m",
            adopt_message_ref=lc.release_message_ref(),
            adopt_pending_progress=lc,
        )
        assert successor.message_ref is _SENTINEL_REF
    final_embeds = current.get("embeds", [])
    if "embed" in current:
        final_embeds = [current["embed"]] if current["embed"] else []
    final_content = current.get("content")
    post_count = len(sends)
    if lc._terminal_reassert_task is not None:
        await asyncio.wait_for(lc._terminal_reassert_task, 1)
    if ending == "recovered":
        assert lc._terminal_reassert_task is None or lc._terminal_reassert_task.done()
        await successor.on_terminal_success(_make_success_state("Recovered answer"))
        assert current.get("content") == "Recovered answer"
        assert len(sends) == post_count
        return
    if "embed" in current:
        assert current["embed"] is None
    else:
        assert current["embeds"] == final_embeds
    assert current["view"] is None
    assert current.get("content") == final_content
    assert len(sends) == post_count
    assert not lc._progress_edits


@pytest.mark.parametrize("missing", [False, True])
async def test_progress_settled_before_terminal_or_missing_needs_no_repair(
    missing: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle_module, "_PROGRESS_SETTLE_S", 0.01)
    started, release = asyncio.Event(), asyncio.Event()
    edit_count = 0

    async def send(**kwargs: Any) -> object:
        return _SENTINEL_REF

    async def edit(ref: Any, **kwargs: Any) -> None:
        nonlocal edit_count
        edit_count += 1
        if edit_count == 1:
            started.set()
            await release.wait()
            if missing:
                raise discord.NotFound(
                    types.SimpleNamespace(status=404, reason="Not Found"),
                    {"code": 10008, "message": "Unknown Message"},
                )

    lc = DiscordTurnLifecycle(send=send, edit=edit, agent_name="test", model_id="m")
    await lc.post_initial()
    lc._last_flush = time.monotonic() - 11
    tick = asyncio.create_task(lc.on_render(_running_tool_turn()))
    await asyncio.wait_for(started.wait(), 1)
    pending = set(lc._progress_edits)
    lc.on_render_stopped()
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    if not missing:
        # It finishes during the settle window, before any terminal edit.
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending), 1)
    if missing:
        finish = asyncio.create_task(lc.on_terminal_success(_make_success_state("Answer")))
        await asyncio.wait_for(finish, 1)
        assert not release.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending), 1)
    else:
        await lc.on_terminal_success(_make_success_state("Answer"))
    assert lc._terminal_reassert_task is None
    assert edit_count == 3, "one progress edit, terminal summary, answer; no repair"


async def test_abandoned_render_does_not_leave_a_progress_task_waiting_for_terminal() -> None:
    release, started = asyncio.Event(), asyncio.Event()

    async def send(**kwargs: Any) -> object:
        return _SENTINEL_REF

    async def edit(ref: Any, **kwargs: Any) -> None:
        started.set()
        await release.wait()

    lc = DiscordTurnLifecycle(send=send, edit=edit, agent_name="test", model_id="m")
    await lc.post_initial()
    lc._last_flush = time.monotonic() - 11
    tick = asyncio.create_task(lc.on_render(_running_tool_turn()))
    await asyncio.wait_for(started.wait(), 1)
    pending = set(lc._progress_edits)
    lc.on_render_stopped()
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    release.set()
    await asyncio.wait_for(asyncio.gather(*pending), 1)
    assert not lc._progress_edits
    assert lc._terminal_reassert_task is None


async def test_terminal_edit_has_an_application_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_module, "_TERMINAL_EDIT_S", 0.01)
    started, release = asyncio.Event(), asyncio.Event()

    async def send(**kwargs: Any) -> object:
        return _SENTINEL_REF

    async def edit(ref: Any, **kwargs: Any) -> None:
        started.set()
        await release.wait()

    lc = DiscordTurnLifecycle(send=send, edit=edit, agent_name="test", model_id="m")
    await lc.post_initial()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(lc.end_card("Done"), 1)
    assert started.is_set()
    assert len(lc._card_writes.terminal_tasks) == 1
    release.set()
    await asyncio.wait_for(asyncio.gather(*lc._card_writes.terminal_tasks), 1)
    assert lc._card_writes.key(_SENTINEL_REF) in lc._card_writes.terminal_ready


async def test_terminal_deadline_does_not_close_discord_global_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle_module, "_TERMINAL_EDIT_S", 0.01)
    client = HTTPClient(asyncio.get_running_loop())
    client._global_over = asyncio.Event()
    client._global_over.set()

    class Response:
        def __init__(self, status: int) -> None:
            self.status = status
            self.reason = "rate limited" if status == 429 else "OK"
            self.headers = {"content-type": "application/json", "Via": "fake-discord"}

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def text(self, **kwargs: Any) -> str:
            return json.dumps({"global": True, "retry_after": 0.05}) if self.status == 429 else "{}"

    class Session:
        requests = 0

        def request(self, *args: Any, **kwargs: Any) -> Response:
            self.requests += 1
            return Response(429 if self.requests == 1 else 200)

    session = Session()
    client._HTTPClient__session = session

    async def send(**kwargs: Any) -> object:
        return _SENTINEL_REF

    async def edit(ref: Any, **kwargs: Any) -> None:
        await client.request(
            Route(
                "PATCH", "/channels/{channel_id}/messages/{message_id}", channel_id=20, message_id=1
            ),
            json={"content": "Done"},
        )

    lc = DiscordTurnLifecycle(send=send, edit=edit, agent_name="test", model_id="m")
    await lc.post_initial()
    with pytest.raises(TimeoutError):
        await lc.end_card("Done")
    assert not client._global_over.is_set()
    await asyncio.wait_for(asyncio.gather(*lc._card_writes.terminal_tasks), 1)
    assert client._global_over.is_set()
    await asyncio.wait_for(client.request(Route("GET", "/channels/{channel_id}", channel_id=99)), 1)
    assert session.requests == 3


@pytest.mark.parametrize("progress_first", [False, True])
async def test_uncertain_terminal_repairs_late_progress(
    monkeypatch: pytest.MonkeyPatch, progress_first: bool
) -> None:
    monkeypatch.setattr(lifecycle_module, "_TERMINAL_EDIT_S", 0.01)
    monkeypatch.setattr(lifecycle_module, "_PROGRESS_SETTLE_S", 0.01)
    progress_started, release_progress = asyncio.Event(), asyncio.Event()
    terminal_applied, release_terminal = asyncio.Event(), asyncio.Event()
    current: dict[str, Any] = {}
    sends = 0
    terminal_edits = 0

    async def send(**kwargs: Any) -> object:
        nonlocal sends
        sends += 1
        current.update(kwargs)
        return _SENTINEL_REF

    async def edit(ref: Any, **kwargs: Any) -> None:
        nonlocal terminal_edits
        embeds = kwargs.get("embeds", [])
        if embeds and embeds[0].title and "Working" in embeds[0].title:
            progress_started.set()
            await release_progress.wait()
            current.update(kwargs)
            return
        terminal_edits += 1
        current.update(kwargs)  # HTTP 200 has applied the terminal card.
        if terminal_edits == 1:
            terminal_applied.set()
            await release_terminal.wait()  # discord.py can hold its lock after 200.

    lc = DiscordTurnLifecycle(send=send, edit=edit, agent_name="test", model_id="m")
    await lc.post_initial()
    lc._last_flush = time.monotonic() - 11
    tick = asyncio.create_task(lc.on_render(_running_tool_turn()))
    await asyncio.wait_for(progress_started.wait(), 1)
    pending = set(lc._progress_edits)
    lc.on_render_stopped()
    tick.cancel()
    await asyncio.gather(tick, return_exceptions=True)
    with pytest.raises(TimeoutError):
        await lc.end_card("Done")
    await terminal_applied.wait()
    if progress_first:
        release_progress.set()
        await asyncio.wait_for(asyncio.gather(*pending), 1)
    release_terminal.set()
    await asyncio.wait_for(asyncio.gather(*lc._card_writes.terminal_tasks), 1)
    if not progress_first:
        release_progress.set()
        await asyncio.wait_for(asyncio.gather(*pending), 1)
    if lc._terminal_reassert_task is not None:
        await asyncio.wait_for(lc._terminal_reassert_task, 1)
    assert current["content"] == "Done"
    assert current["view"] is None
    assert terminal_edits == 2
    assert sends == 1


def _make_lifecycle(
    agent_name: str = "test-agent",
    cancel_view: discord.ui.View | None = None,
    model_id: str = "claude-sonnet-4-6",
    notify_on_completion: bool = False,
    render_tables: bool = False,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    tenant_id: uuid.UUID | None = None,
    budget_channel_id: str | None = None,
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
        budget_channel_id=budget_channel_id,
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


@pytest.mark.parametrize("partial_text", ["", "Partial analysis before cancellation."])
async def test_interrupted_tool_turn_shows_cancelled_and_preserves_partial_answer(
    partial_text: str,
) -> None:
    lc, sends, edits = _make_lifecycle(notify_on_completion=True)
    await lc.post_initial()
    content = [
        ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={})
    ]
    if partial_text:
        content.append(TextBlock(kind="text", text=partial_text))

    await lc.on_terminal_success(
        TurnState(content=content, termination=TerminationReason.INTERRUPTED)
    )

    rendered = "\n".join(
        str(kwargs.get("content", "")) for kwargs in sends + [kwargs for _, kwargs in edits]
    )
    assert "Stopped.\nSend a message to start again." in rendered
    if partial_text:
        assert partial_text in rendered
    assert "<@123>" not in rendered, "cancellation must not send a completion ping"
    assert not lc.was_answered


@pytest.mark.parametrize("status", [400, 429])
async def test_spend_limit_posts_notice_and_error_log(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = uuid.uuid4()
    alerts: list[str] = []
    monkeypatch.setattr(
        lifecycle_module, "alert_ops", lambda url, *, key, message: alerts.append(key)
    )
    lc, _, edits = _make_lifecycle(tenant_id=tenant_id)
    await lc.post_initial()
    body = (
        {"type": "rate_limit_error", "details": {"error_code": "enforced_spend_limit_reached"}}
        if status == 429
        else {
            "type": "invalid_request_error",
            "message": "You have reached your specified API usage limits",
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
    embed = edits[-1][1]["embeds"][0]
    assert embed.fields[0].value.strip() == "Daimon has reached its usage limit."
    assert embed.description == "Ask the team running it to check the limit."
    assert str(tenant_id) not in str(embed.to_dict()), "no tenant id in chat"
    assert {
        "event": "anthropic.spend_limit_reached",
        "log_level": "error",
        "tenant_id": str(tenant_id),
        "limit": "org_cap" if status == 429 else "user_limit",
    } in logs


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
        assert embed.title == "Working on it…", (
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

    async def test_long_answer_carries_the_summary_on_its_last_message(self) -> None:
        """The summary line closes the answer: it leaves the first chunk for the last."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        initial_sends = len(sends)

        state = TurnState(content=[TextBlock(kind="text", text="x" * 4000)])
        await lc.on_terminal_success(state)

        first = edits[-1][1]
        assert first.get("content", "").startswith("x"), "the first chunk replaces the card"
        assert first.get("embeds") == [], "the first chunk drops the summary"
        overflow = sends[initial_sends:]
        assert len(overflow) >= 2, "4,000 characters overflow into at least two more messages"
        assert all("embeds" not in send for send in overflow[:-1]), "middle chunks stay bare"
        footer = overflow[-1]["embeds"][0].footer.text
        assert footer is not None and footer.startswith("test-agent"), (
            "the last chunk carries the summary"
        )

    async def test_long_answer_takes_the_vote_emoji_on_its_last_message(self) -> None:
        sent = iter(range(1, 10))

        async def send(**kwargs: Any) -> object:
            return types.SimpleNamespace(id=next(sent))

        async def edit(ref: Any, **kwargs: Any) -> None:
            pass

        lc = DiscordTurnLifecycle(send=send, edit=edit, agent_name="test-agent", model_id="m")
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="x" * 4000)]))

        assert lc.final_message_id == "1", "the answer still starts on the card"
        assert lc.feedback_message_id == "3", "the vote emoji go on the last chunk"

    async def test_short_answer_keeps_the_summary_on_its_one_message(self) -> None:
        lc, _sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())

        await lc.on_terminal_success(_make_success_state())

        assert edits[-1][1]["embeds"] == [lc._terminal_embed], (
            "the answer reasserts the summary if a progress edit landed in between"
        )

    async def test_completion_ping_answer_carries_the_summary_and_the_card_goes(self) -> None:
        deleted: list[object] = []
        card, answer = types.SimpleNamespace(id=1), types.SimpleNamespace(id=2)
        sends: list[dict[str, Any]] = []

        async def send(**kwargs: Any) -> object:
            sends.append(kwargs)
            return card if len(sends) == 1 else answer

        async def edit(ref: Any, **kwargs: Any) -> None:
            pass

        async def delete(ref: Any) -> None:
            deleted.append(ref)

        lc = DiscordTurnLifecycle(
            send=send,
            edit=edit,
            delete=delete,
            agent_name="test-agent",
            model_id="m",
            requester_id=123,
            notify_on_completion=True,
        )
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        await lc.on_terminal_success(_make_success_state())

        footer = sends[-1]["embeds"][0].footer.text
        assert footer is not None and footer.startswith("test-agent"), "the answer has it"
        assert deleted == [card], "the card above the answer is removed"

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
    assert embed.title == "Something went wrong."
    assert embed.description == (
        "Try again. If it keeps happening, tell an admin."
        if reason is TerminationReason.UPSTREAM
        else notice.next_step.replace("share the request id with an admin", "ask an admin for help")
    )
    if reason is not TerminationReason.UPSTREAM:
        assert notice.cause in embed.fields[0].value
    else:
        assert "may still arrive" in embed.fields[0].value
    assert "**Next:**" not in embed.fields[0].value
    assert "fit_model" not in embed.fields[0].value, "tool details stay in logs"
    assert "rid:" not in embed.fields[0].value
    assert len(embed.fields) == 1, "the numbers ride the footer, not a Details field"
    assert "xxx" not in str(embed.to_dict()), "raw error stays in the logs"


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
    assert "and 42 more" in embed.fields[0].value
    assert "rid:" not in embed.fields[0].value
    assert len(embed.fields) == 1, "the numbers ride the footer, not a Details field"


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
    assert embed.title == "Something went wrong."
    # The fallback joins its lines with a blank line; Discord drops the trailing one.
    assert embed.fields[0].value.rstrip() == "Something went wrong on our side."
    assert embed.description == "Try again. If it keeps happening, tell an admin."
    assert not any("upstream timeout" in str(field.value) for field in embed.fields)


async def test_the_card_reuses_the_rid_bound_for_the_turn() -> None:
    lc, _, edits = _make_lifecycle()
    await lc.on_render(TurnState())

    from structlog.testing import capture_logs

    with capture_logs() as logs, structlog.contextvars.bound_contextvars(rid="01BOUNDRID"):
        await lc.on_terminal_failure(TurnState(), Exception("x"))

    assert "01BOUNDRID" not in str(edits[-1][1]["embeds"][0].to_dict())
    assert any(entry.get("request_id") == "01BOUNDRID" for entry in logs)


async def test_terminal_failure_without_a_reason_on_the_state_maps_the_error() -> None:
    lc, _, edits = _make_lifecycle()
    await lc.on_render(TurnState())

    await lc.on_terminal_failure(TurnState(), TurnError(kind="connection_lost"))

    assert edits[-1][1]["embeds"][0].title == "Something went wrong."


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
    async def test_early_answer_labels_only_first_chunk_on_bot_fallback(self) -> None:
        lc, sends, _ = _make_lifecycle()
        lc._fallback_active = lambda: True  # pyright: ignore[reportPrivateUsage]
        await lc.on_render(_sealed_state("x" * 2100))
        chunks = [sent["content"] for sent in sends if "content" in sent]
        assert len(chunks) > 1
        assert chunks[0].startswith("-# test-agent\nx")
        assert all(not chunk.startswith("-# test-agent\n") for chunk in chunks[1:])
        assert all(len(chunk) <= 2000 for chunk in chunks)
        assert "".join(chunks).removeprefix("-# test-agent\n") == "x" * 2100

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

    @pytest.mark.parametrize("already_flushed", [False, True])
    async def test_terminal_failure_persists_sealed_text_before_error_card(
        self, already_flushed: bool
    ) -> None:
        """Review #655: reproduce stop -> final render -> failure from the driver."""
        operations: list[tuple[str, dict[str, Any]]] = []

        async def send(**kwargs: Any) -> object:
            operations.append(("send", kwargs))
            return _SENTINEL_REF

        async def edit(ref: Any, **kwargs: Any) -> None:
            operations.append(("edit", kwargs))

        lc = DiscordTurnLifecycle(send=send, edit=edit, agent_name="test", model_id="m")
        await lc.post_initial()
        answer = "y" * 600
        state = _sealed_state(answer)
        if already_flushed:
            await lc.on_render(state)
        lc.on_render_stopped()
        await lc.on_render(state)
        err = TurnError(kind="upstream", message="boom")
        state = dataclasses.replace(state, error=err)
        await lc.on_terminal_failure(state, err)
        sealed = [
            index for index, (_, kwargs) in enumerate(operations) if kwargs.get("content") == answer
        ]
        assert len(sealed) == 1, "last-window sealed answer posts once even on failure"
        assert sealed[0] < len(operations) - 1
        assert operations[-1][0] == "edit"
        assert operations[-1][1]["embeds"][0].to_dict()["color"] == COLOR_RED

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
        assert cancel_edit[1].get("content") == "Stopped.\nSend a message to start again.", (
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
        assert embeds[0].title == "Working on it…"
        assert "🔍 Search issues (tracker)" in embeds[0].fields[0].value
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
        assert "embeds" in edits[0][1], "terminal flush should have embed"
        assert edits[-1][1]["embed"].description == "Done."

    async def test_truly_cancelled_turn_shows_turn_cancelled(self) -> None:
        """Empty content (no blocks at all) still shows 'Turn cancelled.'."""
        lc, sends, edits = _make_lifecycle()
        await lc.on_sse_event(_thinking_event())

        state = TurnState()  # completely empty content
        await lc.on_terminal_success(state)

        cancel_edit = edits[-1]
        assert cancel_edit[1].get("content") == "Stopped.\nSend a message to start again."
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
        assert embeds[0].title == "Working on it…"
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


def _terminal_footer(edits: list[tuple[Any, dict[str, Any]]]) -> str:
    """The terminal embed's one summary line."""
    text = _terminal_embed(edits).footer.text
    assert text is not None
    return text


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
    assert _terminal_footer(edits).endswith(f"{GAP}$12.50 left")

    async with db_session_factory() as s, s.begin():
        await set_funding_mode(s, tenant_id=tenant.id, funding_mode="operator_funded")
    lc, _sends, edits = _make_lifecycle(sessionmaker=db_session_factory, tenant_id=tenant.id)
    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())
    await lc.on_terminal_success(_make_success_state())
    assert "left" not in _terminal_footer(edits)


def _window_fixture() -> tuple[DiscordTurnLifecycle, list[tuple[Any, dict[str, Any]]]]:
    """A lifecycle whose card is message 100 and whose sends post messages 101, 102, ..."""
    edits: list[tuple[Any, dict[str, Any]]] = []
    next_id = 100

    async def send(**kwargs: Any) -> object:
        nonlocal next_id
        message = types.SimpleNamespace(id=next_id, kwargs=kwargs)
        next_id += 1
        return message

    async def edit(ref: Any, **kwargs: Any) -> object:
        edits.append((ref, kwargs))
        return types.SimpleNamespace(id=ref.id, edited_at=datetime.now(UTC))

    lc = DiscordTurnLifecycle(send=send, edit=edit, agent_name="test-agent", model_id="m")
    return lc, edits


@pytest.mark.asyncio
async def test_the_answer_keeps_its_summary_and_takes_the_files() -> None:
    """The summary, the vote emoji and the turn's files all sit on the answer."""
    lc, edits = _window_fixture()
    await lc.on_sse_event(_thinking_event())
    await lc.on_terminal_success(_make_success_state())

    answer = lc.answer_message
    assert answer is not None
    assert answer.message_id == 100, "a one-chunk answer is the card, edited in place"
    assert lc.feedback_message_id == "100", "the vote emoji go on the same message"
    assert all(kwargs.get("embeds") != [] for _ref, kwargs in edits), (
        "nothing takes the summary off the answer"
    )

    await answer.edit(types.SimpleNamespace(id=100), attachments=[])
    assert edits[-1][1] == {"attachments": []}, "the sweep edits through the turn's own edit"


@pytest.mark.asyncio
async def test_a_long_answers_files_go_on_its_last_chunk() -> None:
    lc, _edits = _window_fixture()
    await lc.on_sse_event(_thinking_event())
    await lc.on_terminal_success(_make_success_state("x" * 4000))

    answer = lc.answer_message
    assert answer is not None
    assert answer.message_id > 100, "the summary moved to the last chunk at delivery"
    assert lc.feedback_message_id == str(answer.message_id)


@pytest.mark.asyncio
async def test_a_tool_only_turn_takes_its_files_on_the_done_card() -> None:
    lc, edits = _window_fixture()
    await lc.on_sse_event(_thinking_event())
    tool = ToolUseBlock(
        kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}, status="complete"
    )
    await lc.on_terminal_success(TurnState(content=[tool]))

    answer = lc.answer_message
    assert answer is not None and answer.message_id == 100
    card = edits[-1][1]["embed"]
    assert card.description == "Done.", "the card stays as Done."
    assert card.footer.text.startswith("test-agent"), "and keeps its summary line"


@pytest.mark.asyncio
async def test_a_stopped_turn_has_no_answer_for_files() -> None:
    lc, _edits = _window_fixture()
    await lc.on_sse_event(_thinking_event())
    await lc.on_terminal_success(TurnState(content=[]))

    assert lc.answer_message is None, "the sweep posts files on their own"


@pytest.mark.asyncio
async def test_the_turn_window_closes_on_discords_clock() -> None:
    """The bound comes from Discord's own stamps, so a fast host clock cannot widen it."""
    lc, _edits = _window_fixture()
    await lc.on_sse_event(_thinking_event())
    await lc.on_terminal_success(_make_success_state())

    window = lc.turn_window
    assert window is not None
    assert window[0] == 100, "the window opens at the card"
    host_now = discord.utils.time_snowflake(datetime.now(UTC), high=True)
    assert window[1] <= host_now + 1, "it closes at the last Discord stamp the turn saw"


@pytest.mark.asyncio
async def test_an_unstamped_terminal_falls_back_to_the_host_clock() -> None:
    """With no Discord stamp at the end (edits that return nothing), the host clock
    closes the window, so a file the agent posted mid-turn still counts as the turn's."""
    card = types.SimpleNamespace(id=100)

    async def send(**kwargs: Any) -> object:
        return card

    async def edit(ref: Any, **kwargs: Any) -> None:
        pass

    lc = DiscordTurnLifecycle(send=send, edit=edit, agent_name="test-agent", model_id="m")
    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())
    mid_turn_post = discord.utils.time_snowflake(datetime.now(UTC))
    await lc.on_terminal_success(_make_success_state())

    window = lc.turn_window
    assert window is not None
    assert window[0] < mid_turn_post < window[1], "the stale card id must not close it"


@pytest.mark.asyncio
async def test_a_tool_only_turn_still_closes_its_window() -> None:
    """A tool-only turn posts files too; its sweep needs the window for the dedup."""
    lc, _edits = _window_fixture()
    await lc.on_sse_event(_thinking_event())
    tool = ToolUseBlock(
        kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}, status="complete"
    )
    await lc.on_terminal_success(TurnState(content=[tool]))

    assert lc.turn_window is not None


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
        assert cancel_edit[1].get("content") == "Stopped.\nSend a message to start again.", (
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
        assert f"{GAP}$" in _terminal_footer(edits)
        assert _terminal_footer(edits).endswith(" used")

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
        assert "used" not in _terminal_footer(edits)

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
        assert expected is not None
        assert f"{GAP}{expected} used" in _terminal_footer(edits), (
            f"footer cost must equal the billing-ledger cost {expected} to the cent"
        )


@pytest.mark.asyncio
async def test_haiku_footer_adds_cost_of_each_short_request() -> None:
    lc, _sends, edits = _make_lifecycle(model_id="claude-haiku-5-5")
    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())
    state = TurnState()
    for event_id in ("short_1", "short_2"):
        state = apply(
            state,
            _span_usage_event(
                event_id=event_id,
                input_tokens=60_000,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                output_tokens=0,
            ),
        )
    await lc.on_terminal_success(state)
    assert f"{GAP}$0.012 used" in _terminal_footer(edits), (
        "two 60k-token requests must each pay the short-prompt rate"
    )


@pytest.mark.asyncio
async def test_footer_cost_is_the_debit_with_markup() -> None:
    """`used` is what the tenant is debited, markup included, so it agrees with `left`."""
    sends: list[dict[str, Any]] = []

    async def send(**kwargs: Any) -> object:
        sends.append(kwargs)
        return _SENTINEL_REF

    async def edit(ref: Any, **kwargs: Any) -> None:
        sends.append(kwargs)

    lc = DiscordTurnLifecycle(
        send=send,
        edit=edit,
        agent_name="test-agent",
        model_id="claude-sonnet-4-6",
        markup=Decimal("1.1"),
    )
    await lc.on_sse_event(_thinking_event())
    await lc.on_render(TurnState())
    usage = dict(input_tokens=10000, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    state = apply(TurnState(), _span_usage_event(event_id="u1", output_tokens=3000, **usage))
    await lc.on_terminal_success(state)

    raw = cost_of(
        BetaManagedAgentsSpanModelUsage(output_tokens=3000, speed="standard", **usage),
        MODEL_PRICING["claude-sonnet-4-6"],
    )
    debit = format_cost(float(debit_amount(raw, markup=Decimal("1.1"))))
    footer = [kw["embeds"][0].footer.text for kw in sends if kw.get("embeds")][-1]
    assert f"{GAP}{debit} used" in footer, f"footer must show the debit {debit}"
    assert format_cost(raw) != debit, "the raw cost would understate it"


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

    async def test_failed_delete_keeps_card_intent_recoverable(self) -> None:
        lc, sends, _, _ = _make_unprompted_lifecycle()

        async def fail_delete(_message: object) -> None:
            raise discord.HTTPException(
                types.SimpleNamespace(status=503, reason="Service Unavailable"),
                "delete unavailable",
            )

        lc._delete = fail_delete  # pyright: ignore[reportPrivateUsage]
        await lc.on_render(TurnState(content=[TextBlock(kind="text", text="partial")]))
        await lc.on_terminal_success(TurnState())

        assert len(sends) == 1
        assert lc.card_discard_failed
        assert lc._card_message_ref is _SENTINEL_REF  # pyright: ignore[reportPrivateUsage]

    async def test_missing_delete_does_not_mark_card_discard_failed(self) -> None:
        lc, _, _, _ = _make_unprompted_lifecycle()

        async def missing_delete(_message: object) -> None:
            raise discord.NotFound(
                types.SimpleNamespace(status=404, reason="Not Found"),
                {"code": 10008, "message": "Unknown Message"},
            )

        lc._delete = missing_delete  # pyright: ignore[reportPrivateUsage]
        await lc.on_render(TurnState(content=[TextBlock(kind="text", text="partial")]))
        await lc.on_terminal_success(TurnState())

        assert not lc.card_discard_failed

    async def test_missing_webhook_delete_keeps_card_intent_recoverable(self) -> None:
        lc, _, _, _ = _make_unprompted_lifecycle()

        async def missing_webhook(_message: object) -> None:
            raise discord.NotFound(
                types.SimpleNamespace(status=404, reason="Not Found"),
                {"code": 10015, "message": "Unknown Webhook"},
            )

        lc._delete = missing_webhook  # pyright: ignore[reportPrivateUsage]
        await lc.on_render(TurnState(content=[TextBlock(kind="text", text="partial")]))
        await lc.on_terminal_success(TurnState())

        assert lc.card_discard_failed

    async def test_client_delete_failure_keeps_card_intent_recoverable(self) -> None:
        lc, _, _, _ = _make_unprompted_lifecycle()

        async def fail_delete(_message: object) -> None:
            raise discord.ClientException("cannot delete")

        lc._delete = fail_delete  # pyright: ignore[reportPrivateUsage]
        await lc.on_render(TurnState(content=[TextBlock(kind="text", text="partial")]))
        await lc.on_terminal_success(TurnState())

        assert lc.card_discard_failed

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
@pytest.mark.parametrize("managed", [False, True])
async def test_reactions_replace_accepted_after_success(enabled, managed):
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
        acknowledgment_managed=managed,
    )
    await lifecycle.on_acknowledgment("accepted")
    await lifecycle.on_terminal_success(_make_success_state())
    await lifecycle.on_acknowledgment("done")
    expected = [("add", "✅")] if managed else [("add", "👀"), ("add", "✅"), ("remove", "👀")]
    assert calls == (expected if enabled else [])


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


@pytest.mark.asyncio
async def test_terminal_footer_shows_an_active_channel_budgets_remainder(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("12.50"))
    await make_channel_budget(
        db_session, tenant=tenant, channel_id="C1", window="total", limit_usd=Decimal("5")
    )
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
        lc, _sends, edits = _make_lifecycle(
            sessionmaker=db_session_factory, tenant_id=tenant.id, budget_channel_id=channel
        )
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        await lc.on_terminal_success(_make_success_state())
        footers[channel] = _terminal_footer(edits)
    assert footers["C1"].endswith(f"{GAP}$3.75 left"), "the budget's remainder"
    for channel in ("C2", "C3", None):
        assert footers[channel].endswith(f"{GAP}$11.25 left"), (
            f"{channel}: an inactive or missing budget shows the tenant balance"
        )


@pytest.mark.asyncio
async def test_answer_keeps_the_channel_budget_footer_on_the_visible_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await make_channel_budget(
        db_session, tenant=tenant, channel_id="C1", window="total", limit_usd=Decimal("25")
    )
    await db_session.commit()
    lc, _sends, edits = _make_lifecycle(
        sessionmaker=db_session_factory, tenant_id=tenant.id, budget_channel_id="C1"
    )
    await lc.post_initial()
    await lc.on_terminal_success(_make_success_state())

    assert _terminal_footer(edits).endswith(f"{GAP}$25.00 left")
    final_edit = edits[-1][1]
    assert final_edit["content"] == "Hello response"
    assert "embed" not in final_edit, "editing the answer must retain the terminal embed"
    assert await lc.prepend_revealed_answer("Recovered files.")
    assert "embed" not in edits[-1][1], "a later answer edit must retain the footer too"


@pytest.mark.parametrize("missing_at", ["render", "terminal", "answer"])
@pytest.mark.parametrize("render_tables", [False, True])
async def test_missing_card_is_replaced_before_answer_delivery(missing_at, render_tables):
    from structlog.testing import capture_logs

    sent = []
    replacements = []
    refs = iter([types.SimpleNamespace(id=1000), types.SimpleNamespace(id=1001)])
    clock = [0.0]
    missing = [False]

    async def send(**kwargs):
        ref = next(refs)
        sent.append((ref, kwargs))
        return ref

    async def edit(ref, **kwargs):
        is_target = (
            (missing_at == "render" and not lifecycle._terminal)
            or (missing_at == "terminal" and lifecycle._terminal and "content" not in kwargs)
            or (missing_at == "answer" and "content" in kwargs)
        )
        if is_target and not missing[0]:
            missing[0] = True
            raise discord.NotFound(
                types.SimpleNamespace(status=404, reason="Not Found"),
                {"code": 10008, "message": "Unknown Message"},
            )
        ref.kwargs = kwargs

    async def record_replacement(ref):
        replacements.append(ref.id)

    lifecycle = DiscordTurnLifecycle(
        send=send,
        edit=edit,
        agent_name="test",
        model_id="claude-sonnet-4-6",
        clock=lambda: clock[0],
        on_replacement=record_replacement,
        render_tables=render_tables,
    )
    text = (
        "| Name | Value |\n| --- | ---: |\n| Example | 42 |"
        if render_tables
        else "Complete answer."
    )
    with capture_logs() as logs:
        await lifecycle.post_initial()
        clock[0] = 11.0
        await lifecycle.on_render(_make_success_state(text))
        await lifecycle.on_terminal_success(_make_success_state(text))
    assert len(sent) == 2, "replace the missing card once, without posting an error"
    assert replacements == [1001], "record the replacement for reply routing"
    assert lifecycle.final_message_id == lifecycle.feedback_message_id == "1001"
    assert lifecycle.card_message_id == "1000", "retire the original durable card intent"
    assert lifecycle.was_answered
    answer = sent[-1][1] if missing_at == "answer" else sent[-1][0].kwargs
    assert ("table-1.png" if render_tables else text) in answer["content"]
    if missing_at == "answer":
        assert answer["embeds"], "a replacement answer must keep its summary"
        assert "attachments" not in answer and "view" not in answer
        if render_tables:
            assert answer["files"][0].filename == "table-1.png"
    assert any(
        entry["event"] == "turn.message_missing" and not entry["delivered"] for entry in logs
    )


async def test_missing_message_after_answer_delivery_is_a_logged_noop():
    from structlog.testing import capture_logs

    sent = []
    missing = [False]
    ref = types.SimpleNamespace(id=1000)

    async def send(**kwargs):
        sent.append(kwargs)
        return ref

    async def edit(message, **kwargs):
        if missing[0]:
            raise discord.NotFound(
                types.SimpleNamespace(status=404, reason="Not Found"),
                {"code": 10008, "message": "Unknown Message"},
            )

    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="test", model_id="claude-sonnet-4-6"
    )
    await lifecycle.post_initial()
    await lifecycle.on_terminal_success(_make_success_state())
    missing[0] = True
    with capture_logs() as logs:
        await lifecycle.prepend_revealed_answer("Recovered files.")
    assert len(sent) == 1, "a stale edit must not duplicate an already delivered answer"
    assert any(entry["event"] == "turn.message_missing" and entry["delivered"] for entry in logs)


@pytest.mark.parametrize("code,status", [(10015, 404), (50013, 403), (0, 500)])
async def test_other_discord_edit_errors_still_propagate(code, status):
    lc, sends, _ = _make_lifecycle()
    await lc.post_initial()

    async def edit(ref, **kwargs):
        raise discord.HTTPException(
            types.SimpleNamespace(status=status, reason="Failure"),
            {"code": code, "message": "Failure"},
        )

    lc._edit = edit
    with pytest.raises(discord.HTTPException):
        await lc.on_terminal_success(_make_success_state())
    assert len(sends) == 1


async def test_setup_notice_replaces_a_deleted_initial_card():
    sent = []
    refs = iter([types.SimpleNamespace(id=1000), types.SimpleNamespace(id=1001)])

    async def send(**kwargs):
        sent.append(kwargs)
        return next(refs)

    async def edit(message, **kwargs):
        raise discord.NotFound(
            types.SimpleNamespace(status=404, reason="Not Found"),
            {"code": 10008, "message": "Unknown Message"},
        )

    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="test", model_id="claude-sonnet-4-6"
    )
    await lifecycle.post_initial()
    await lifecycle.edit_card(content="Send your message again.", embed=None, view=None)
    assert sent[-1] == {"content": "Send your message again."}
    assert lifecycle.final_message_id == "1001"


@pytest.mark.parametrize(
    ("status", "cause"),
    [(503, "Daimon couldn't reach its AI service."), (529, "Daimon's AI service is busy.")],
)
async def test_terminal_overload_is_plain_and_allows_for_later_file_delivery(
    status: int, cause: str
) -> None:
    lc, _, edits = _make_lifecycle()
    await lc.on_render(TurnState())
    from anthropic import APIStatusError

    error = APIStatusError(
        message="{'type': 'error', 'request_id': 'req_private', 'message': 'Overloaded'}",
        response=httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com")),
        body={"request_id": "req_private"},
    )
    await lc.on_terminal_failure(
        TurnState(termination=TerminationReason.UPSTREAM), TurnError(kind="upstream", cause=error)
    )
    card = edits[-1][1]["embeds"][0]
    text = str(card.to_dict())
    assert cause in card.fields[0].value
    assert card.description == "Try again in a minute."
    assert "may still arrive" in text
    assert "req_private" not in text
    assert "rid:" not in text
    assert "tool calls" not in text
    assert "error'" not in text


@pytest.mark.parametrize("wrapped", [False, True])
async def test_terminal_failure_keeps_authored_setup_guidance(wrapped: bool) -> None:
    lc, _, edits = _make_lifecycle()
    await lc.on_render(TurnState())
    copy = "This turn's setup context expired. Mention me again to continue."
    error: Exception = UserFacingError(copy)
    if wrapped:
        error = TurnError(kind="connection_lost", cause=error)
    await lc.on_terminal_failure(TurnState(), error)
    card = edits[-1][1]["embeds"][0]
    assert card.description == copy
    assert "Try again in a minute" not in str(card.to_dict())
    assert "request id" not in str(card.to_dict())


async def test_ended_only_once_the_terminal_render_reached_discord() -> None:
    """`ended` stays False when the terminal edit fails, so the card can still be ended."""
    lc, _, _ = _make_lifecycle()
    await lc.on_render(TurnState())
    assert not lc.ended

    async def failing_edit(ref: Any, **kwargs: Any) -> None:
        raise discord.HTTPException(types.SimpleNamespace(status=500, reason="x"), "edit failed")  # pyright: ignore[reportArgumentType]

    lc._edit = failing_edit  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(discord.HTTPException):
        await lc.on_terminal_failure(TurnState(), Exception("boom"))
    assert not lc.ended, "the pending card is still on screen"


async def test_ended_after_a_terminal_render() -> None:
    lc, _, _ = _make_lifecycle()
    await lc.on_render(TurnState())
    await lc.on_terminal_failure(TurnState(), Exception("boom"))
    assert lc.ended
