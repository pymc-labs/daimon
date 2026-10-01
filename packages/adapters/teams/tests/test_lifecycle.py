"""TeamsTurnLifecycle: one status card edited in place, then replaced by the answer."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import structlog
from anthropic import RateLimitError
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsTextBlock,
)
from daimon.adapters.teams import card
from daimon.adapters.teams import lifecycle as lifecycle_module
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsTurnLifecycle, TimedSender
from daimon.core.errors import TurnError
from daimon.core.message_split import split_fenced
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from microsoft_teams.api import MessageActivityInput, SentActivity

from .conftest import CONVERSATION_ID, SERVICE_URL, FakeSender


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def _posted(
    sender: FakeSender,
    clock: Clock | None = None,
    *,
    request_id: Callable[[], str] = lambda: "rid-1",
    **kw: Any,
) -> TeamsTurnLifecycle:
    """A lifecycle whose status card is already posted."""
    lifecycle = TeamsTurnLifecycle(
        sender=sender,
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        cancel_key="key-1",
        clock=clock or Clock(),
        request_id=request_id,
        **kw,
    )
    await lifecycle.post_initial()
    return lifecycle


def _answer(text: str) -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=text)])


def _card_json(sender: FakeSender, index: int) -> str:
    return sender.activities[index].model_dump_json(by_alias=True)


async def test_initial_card_carries_the_cancel_key_and_later_renders_edit_it() -> None:
    sender, clock = FakeSender(), Clock()
    lifecycle = await _posted(sender, clock)
    assert lifecycle.message_id == "m-1"
    assert sender.sent[0][0] == CONVERSATION_ID and sender.sent[0][2] == SERVICE_URL
    assert sender.activities[0].id is None
    assert card.CANCEL_VERB in _card_json(sender, 0) and "key-1" in _card_json(sender, 0)

    clock.now += 1
    await lifecycle.on_render(TurnState())
    assert len(sender.sent) == 1, "renders inside the debounce window are skipped"
    clock.now += 5
    await lifecycle.on_render(TurnState())
    assert len(sender.sent) == 2 and sender.activities[1].id == "m-1"


def _card_body(sender: FakeSender, index: int) -> list[dict[str, Any]]:
    return json.loads(_card_json(sender, index))["attachments"][0]["content"]["body"]


async def test_renders_draw_the_turns_tool_lines_and_the_latest_draft() -> None:
    sender, clock = FakeSender(), Clock()
    lifecycle = await _posted(sender, clock)
    message: Any = BetaManagedAgentsAgentMessageEvent(
        id="evt_1",
        type="agent.message",
        processed_at=datetime(2026, 1, 1, tzinfo=UTC),
        content=[BetaManagedAgentsTextBlock(type="text", text="Checking\nthe files.")],
    )
    await lifecycle.on_sse_event(message)
    read = ToolUseBlock(
        kind="tool_use", id="t1", type="agent.tool_use", name="read", input={}, status="complete"
    )
    bash = ToolUseBlock(kind="tool_use", id="t2", type="agent.tool_use", name="bash", input={})
    clock.now += 65
    await lifecycle.on_render(TurnState(content=[read, bash], finished_tool_ids=("t1",)))

    headline, tools, draft, _actions = _card_body(sender, -1)
    assert headline["text"] == "**Working** · 1m 5s", "a running tool makes the turn Working"
    assert tools["text"] == "✔️ Read a file\n\n🖋️ Running a command", "one paragraph per line"
    assert tools["fontType"] == "Monospace", "a TextBlock draws no code fence"
    assert (draft["text"], draft["isSubtle"]) == ("Checking the files.", True), "draft on one line"

    clock.now += 5
    finished = dataclasses.replace(bash, status="complete")
    await lifecycle.on_render(TurnState(content=[read, finished], finished_tool_ids=("t1", "t2")))
    assert _card_body(sender, -1)[0]["text"] == "**Thinking** · 1m 10s", "no tool runs any more"


async def test_answer_replaces_the_card_with_feedback_and_no_usage_footer() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    lifecycle.answer_prefix = "Picked up where we left off."
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    final = sender.activities[-1]
    assert final.id == "m-1"
    assert final.text == "Picked up where we left off.\n\nThe posterior mean is 3.", (
        "the answer alone, as on Discord and Slack"
    )
    assert final.channel_data is not None and final.channel_data.feedback_loop is not None
    assert lifecycle.answer_prefix_applied
    assert lifecycle.card_closed and lifecycle.final_message_id == "m-1"


async def test_a_spend_limit_alerts_the_operators(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(
        lifecycle_module, "alert_ops", lambda url, *, key, message: alerts.append(key)
    )
    tenant_id = uuid.uuid4()
    sender = FakeSender()
    lifecycle = await _posted(sender, tenant_id=tenant_id)
    body = {"type": "rate_limit_error", "details": {"error_code": "enforced_spend_limit_reached"}}
    response = httpx.Response(
        429,
        json={"type": "error", "error": body},
        request=httpx.Request("GET", "https://api.anthropic.com/v1/models"),
    )
    err = TurnError(kind="upstream", cause=RateLimitError("limit", response=response, body=body))
    with structlog.testing.capture_logs() as logs:
        await lifecycle.on_terminal_failure(TurnState(error=err), err)
    assert alerts == ["spend_limit:org_cap"]
    assert "reached its model usage limit" in _card_json(sender, -1)
    assert {
        "event": "anthropic.spend_limit_reached",
        "log_level": "error",
        "tenant_id": str(tenant_id),
        "limit": "org_cap",
    } in logs


async def test_a_long_answer_overflows_into_new_messages_with_feedback_last() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    paragraph = "word " * 1000
    await lifecycle.on_terminal_success(_answer("\n\n".join([paragraph] * 4)))

    chunks = sender.activities[1:]
    assert len(chunks) >= 2
    assert chunks[0].id == "m-1" and all(c.id is None for c in chunks[1:])
    assert all(c.channel_data is None or c.channel_data.feedback_loop is None for c in chunks[:-1])
    assert chunks[-1].channel_data is not None and chunks[-1].channel_data.feedback_loop
    assert lifecycle.final_message_id == f"m-{len(sender.sent)}"


async def test_a_failed_answer_post_collapses_the_card_and_leaves_no_watermark() -> None:
    sender = FakeSender(fail_on={1})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("never seen"))

    assert sender.activities[-1].id == "m-1"
    assert "Something went wrong posting the answer" in _card_json(sender, -1)
    assert lifecycle.card_closed and lifecycle.final_message_id is None


async def test_a_failed_later_part_says_the_answer_may_be_cut_short() -> None:
    sender = FakeSender(fail_on={2})
    lifecycle = await _posted(sender)
    paragraph = "word " * 1000
    await lifecycle.on_terminal_success(_answer("\n\n".join([paragraph] * 4)))

    assert [a.id for a in sender.activities[1:]] == ["m-1", None, None], "answer, lost part, note"
    assert "may be missing" in _card_json(sender, -1)
    assert lifecycle.card_closed and lifecycle.final_message_id is None


async def test_a_timed_out_answer_edit_is_sent_again_not_collapsed() -> None:
    sender = FakeSender(timeout_on={1})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert [a.id for a in sender.activities[1:]] == ["m-1", "m-1"], "the edit is resent"
    assert "The posterior mean is 3." in _card_json(sender, -1)
    assert lifecycle.final_message_id == "m-1"


async def test_an_answer_edit_that_keeps_timing_out_is_never_overwritten() -> None:
    """The edit may have landed, so neither a failure notice nor the boot sweep replaces it."""
    sender = FakeSender(timeout_on={1, 2})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert [a.id for a in sender.activities[1:]] == ["m-1", "m-1", None], "no edit after two"
    assert "timed out" in _card_json(sender, -1), "a new message covers an edit that never landed"
    assert lifecycle.card_closed, "a closed card retires its intent, so the sweep skips it"
    assert lifecycle.final_message_id is None, "no watermark past an answer nobody may have seen"


async def test_a_failed_retry_after_a_timed_out_edit_still_never_collapses_the_card() -> None:
    sender = FakeSender(timeout_on={1}, fail_on={2})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert all(a.id != "m-1" for a in sender.activities[3:]), "the landed edit is never replaced"
    assert "timed out" in _card_json(sender, -1)
    assert lifecycle.card_closed and lifecycle.final_message_id is None


async def test_a_timed_out_cancel_notice_is_resent_then_collapsed_without_an_answer() -> None:
    sender = FakeSender(timeout_on={1})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(TurnState())
    assert [a.id for a in sender.activities[1:]] == ["m-1", "m-1"], "the edit is resent"
    assert card.CANCELLED_NOTICE in _card_json(sender, -1) and lifecycle.card_closed

    sender = FakeSender(timeout_on={1, 2})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(TurnState())
    assert sender.activities[-1].id == "m-1"
    assert "went wrong finishing this turn" in _card_json(sender, -1), "no answer is claimed"
    assert lifecycle.card_closed


async def test_a_late_notice_is_edited_in_above_the_answer() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    final = sender.activities[-1]
    assert final.id == "m-1" and final.text is not None
    assert final.text == "I lost the workspace.\n\nThe posterior mean is 3."
    assert final.channel_data is not None and final.channel_data.feedback_loop is not None, (
        "the feedback buttons stay on a single-message answer"
    )


async def test_a_late_notice_whose_edit_timed_out_counts_as_shown() -> None:
    """A timed-out edit may have landed; sending the notice as well could show it twice."""
    sender = FakeSender(timeout_on={2, 3})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    assert [a.id for a in sender.activities[2:]] == ["m-1", "m-1"], "edited, then retried once"


async def test_a_late_notice_without_an_answer_on_screen_is_sent_on_its_own() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(TurnState(content=[]))

    assert not await lifecycle.prepend_revealed_answer("I lost the workspace.")


async def test_no_answer_reads_as_cancelled_or_done() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(TurnState())
    assert card.CANCELLED_NOTICE in _card_json(sender, -1)

    sender = FakeSender()
    lifecycle = await _posted(sender)
    tool = ToolUseBlock(kind="tool_use", id="t1", type="agent.tool_use", name="bash", input={})
    await lifecycle.on_terminal_success(TurnState(content=[tool]))
    assert _card_text(sender, -1) == card.TOOLS_DONE_NOTICE, "a tool-only turn closes plainly"


def _card_text(sender: FakeSender, index: int) -> str:
    return json.loads(_card_json(sender, index))["attachments"][0]["content"]["body"][0]["text"]


async def test_failure_closes_the_card_with_the_termination_notice_once() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    error = TurnError(kind="upstream", message="overloaded")
    await lifecycle.on_terminal_failure(TurnState(error=error), error)
    await lifecycle.close_with_notice("second notice is ignored")

    assert len(sender.sent) == 2, "the card closes once"
    text = _card_text(sender, 1)
    assert text.startswith("❌ Agent service error: "), "the notice replaces the raw error"
    assert text.endswith("\n\nRequest id: rid-1"), "one paragraph per line, rid last"
    assert lifecycle.card_closed


async def test_a_notice_that_fails_to_build_falls_back_to_the_raw_error() -> None:
    def broken() -> str:
        raise RuntimeError("no id")

    sender = FakeSender()
    lifecycle = await _posted(sender, request_id=broken)
    error = TurnError(kind="upstream", message="overloaded")
    await lifecycle.on_terminal_failure(TurnState(error=error), error)

    assert _card_text(sender, 1) == "❌ overloaded", "the card still closes"


def test_an_oversized_notice_fits_the_teams_limit_and_keeps_the_request_id() -> None:
    notice = render_termination_notice(TerminationReason.UPSTREAM, request_id="rid-1")
    assert notice is not None
    huge = dataclasses.replace(notice, cause="x" * 10_000)

    text = card.termination_text(huge)

    assert len(text) <= card.TEAMS_LIMIT, "Teams rejects an oversized message"
    assert text.endswith("…\n\nRequest id: rid-1"), "clipped before the tail"


class _HungSender:
    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> SentActivity:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_a_hung_send_times_out_as_a_send_error() -> None:
    """A half-open socket must not hold a turn, its slots or the boot sweep forever."""
    sender = TimedSender(_HungSender(), timeout=0.01)
    with pytest.raises(TEAMS_SEND_ERRORS):
        await sender.send(CONVERSATION_ID, MessageActivityInput(text="hi"), service_url=None)


def test_a_long_non_ascii_answer_splits_under_the_teams_payload_cap() -> None:
    chunks = split_fenced("统计" * 5_000, card.TEAMS_LIMIT)
    assert len(chunks) > 1 and all(len(json.dumps(chunk)) < 28_000 for chunk in chunks)


def _unprompted(sender: FakeSender) -> TeamsTurnLifecycle:
    return TeamsTurnLifecycle(
        sender=sender,
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        cancel_key="key-1",
        clock=Clock(),
        unprompted=True,
    )


async def test_an_unprompted_turn_posts_only_its_answer() -> None:
    """Nobody asked: no status card up front, the answer arrives as a new message."""
    sender = FakeSender()
    lifecycle = _unprompted(sender)
    await lifecycle.post_initial()
    await lifecycle.on_render(_answer("draft"))
    assert sender.sent == [], "no card and no render before there is an answer"

    await lifecycle.on_terminal_success(_answer("Thursdays."))
    [answer] = sender.activities
    assert answer.id is None and answer.text == "Thursdays.", "posted, not an edit"
    assert lifecycle.final_message_id == "m-1", "the watermark can move past it"


async def test_an_unprompted_turn_without_an_answer_or_with_a_failure_stays_silent() -> None:
    sender = FakeSender()
    tool = ToolUseBlock(kind="tool_use", id="t1", type="agent.tool_use", name="bash", input={})
    await _unprompted(sender).on_terminal_success(TurnState(content=[tool]))
    error = TurnError(kind="upstream", message="overloaded")
    await _unprompted(sender).on_terminal_failure(TurnState(error=error), error)
    await _unprompted(sender).close_with_notice("Sorry, something went wrong.")
    assert sender.sent == [], "no tool trail, failure card or notice in a thread nobody asked in"
