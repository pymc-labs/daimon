"""TeamsTurnLifecycle: one status card edited in place, then replaced by the answer."""

from __future__ import annotations

import asyncio

import pytest
from daimon.adapters.teams import card
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsTurnLifecycle, TimedSender
from daimon.core.errors import TurnError
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from microsoft_teams.api import MessageActivityInput, SentActivity

from .conftest import CONVERSATION_ID, SERVICE_URL, FakeSender


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def _posted(sender: FakeSender, clock: Clock | None = None) -> TeamsTurnLifecycle:
    """A lifecycle whose status card is already posted."""
    lifecycle = TeamsTurnLifecycle(
        sender=sender,
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        cancel_key="key-1",
        agent_name="daimon",
        model_id="claude-test",
        clock=clock or Clock(),
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


async def test_failure_closes_the_card_with_the_reason_once() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    state = TurnState(error=TurnError(kind="upstream", message="overloaded"))
    await lifecycle.on_terminal_failure(state, RuntimeError("x"))
    await lifecycle.close_with_notice("second notice is ignored")

    assert len(sender.sent) == 2
    assert "❌ overloaded" in _card_json(sender, 1)
    assert lifecycle.card_closed


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
