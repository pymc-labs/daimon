"""TeamsTurnLifecycle: one status card edited in place, then replaced by the answer."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable

import pytest
from daimon.adapters.teams import card
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
) -> TeamsTurnLifecycle:
    """A lifecycle whose status card is already posted."""
    lifecycle = TeamsTurnLifecycle(
        sender=sender,
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        cancel_key="key-1",
        agent_name="daimon",
        model_id="claude-test",
        clock=clock or Clock(),
        request_id=request_id,
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


async def test_answer_replaces_the_card_with_footer_and_feedback() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    lifecycle.answer_prefix = "Picked up where we left off."
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    final = sender.activities[-1]
    assert final.id == "m-1"
    assert final.text is not None
    assert final.text.startswith("Picked up where we left off.\n\nThe posterior mean is 3.")
    assert "daimon · " in final.text
    assert final.channel_data is not None and final.channel_data.feedback_loop is not None
    assert lifecycle.answer_prefix_applied
    assert lifecycle.card_closed and lifecycle.final_message_id == "m-1"


async def test_a_long_answer_overflows_into_new_messages_with_the_footer_last() -> None:
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

    assert len(sender.sent) == 3, "no failure notice follows the two edits"
    assert lifecycle.card_closed, "a closed card retires its intent, so the sweep skips it"
    assert lifecycle.final_message_id is None, "no watermark past an answer nobody may have seen"


async def test_a_late_notice_is_edited_in_above_the_answer() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    final = sender.activities[-1]
    assert final.id == "m-1" and final.text is not None
    assert final.text.startswith("I lost the workspace.\n\nThe posterior mean is 3.")
    assert "daimon · " in final.text, "the footer stays on a single-message answer"


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
    assert "✅ daimon" in _card_json(sender, -1)


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
    assert "\n\nRequest id: rid-1\n\ndaimon · " in text, "one paragraph per line, rid kept"
    assert lifecycle.card_closed


async def test_a_notice_that_fails_to_build_falls_back_to_the_raw_error() -> None:
    def broken() -> str:
        raise RuntimeError("no id")

    sender = FakeSender()
    lifecycle = await _posted(sender, request_id=broken)
    error = TurnError(kind="upstream", message="overloaded")
    await lifecycle.on_terminal_failure(TurnState(error=error), error)

    assert _card_text(sender, 1).startswith("❌ overloaded · daimon"), "the card still closes"


def test_an_oversized_notice_fits_the_teams_limit_and_keeps_the_request_id() -> None:
    notice = render_termination_notice(TerminationReason.UPSTREAM, request_id="rid-1")
    assert notice is not None
    huge = dataclasses.replace(notice, cause="x" * 10_000)

    text = card.termination_text(huge, footer="daimon · 1s")

    assert len(text) <= card.TEAMS_LIMIT, "Teams rejects an oversized message"
    assert text.endswith("…\n\nRequest id: rid-1\n\ndaimon · 1s"), "clipped before the tail"


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
