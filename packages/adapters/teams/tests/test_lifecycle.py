"""TeamsTurnLifecycle: one status card edited in place, then replaced by the answer."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import structlog
from anthropic import RateLimitError
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.teams import card
from daimon.adapters.teams import lifecycle as lifecycle_module
from daimon.adapters.teams.image_preview import MAX_PREVIEWS
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsTurnLifecycle, TimedSender
from daimon.core.errors import TurnError
from daimon.core.message_split import split_fenced
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.tenant_balance import debit_amount
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.outbound import OtherAppsScrubbedLifecycle
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState, UsageTotals
from daimon.core.turn.status_lines import format_summary
from daimon.core.turn.termination import TerminationReason
from microsoft_teams.api import (
    Account,
    Attachment,
    MentionEntity,
    MessageActivityInput,
    SentActivity,
)
from sqlalchemy.exc import OperationalError

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


_TOOL = ToolUseBlock(kind="tool_use", id="t1", type="agent.tool_use", name="bash", input={})


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


def _has_stop_button(sender: FakeSender, index: int) -> bool:
    return card.CANCEL_VERB in _card_json(sender, index)


class GatedSender(FakeSender):
    """A `FakeSender` whose sends from the `held`th on wait for `release`."""

    def __init__(self, held: int) -> None:
        super().__init__()
        self.held, self.entered, self.release = held, asyncio.Event(), asyncio.Event()

    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> SentActivity:
        index = len(self.sent)
        sent = await super().send(conversation_id, activity, service_url=service_url)
        if index == self.held:
            self.entered.set()
            await self.release.wait()
        return sent


async def _settled(lifecycle: TeamsTurnLifecycle) -> None:
    """Let the click's background edit run to its end."""
    task = lifecycle._card_edits.stopping  # pyright: ignore[reportPrivateUsage]
    assert task is not None
    await task


async def test_a_stop_click_edits_the_chat_card_to_stopping_without_any_render() -> None:
    sender, clock = FakeSender(), Clock()
    lifecycle = await _posted(sender, clock)
    clock.now += 1
    stopping = lifecycle.request_stop()
    assert stopping is not None and lifecycle.stopping
    assert len(sender.sent) == 1, "the click itself sends nothing"

    await _settled(lifecycle)
    assert len(sender.sent) == 2 and sender.activities[1].id == "m-1", "one edit of the same card"
    headline, elapsed = _card_body(sender, 1)
    assert headline["text"] == f"**{card.STOPPING_HEADLINE}**" and elapsed["text"] == "1s"
    assert not _has_stop_button(sender, 1)

    clock.now += 5
    await lifecycle.on_render(TurnState(content=[_TOOL]))
    assert not _has_stop_button(sender, -1), "a later render keeps it stopping"
    assert _card_body(sender, -1)[0]["text"] == f"**{card.STOPPING_HEADLINE}**"


async def test_a_double_click_starts_one_edit() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    assert lifecycle.request_stop() is not None
    first = lifecycle._card_edits.stopping  # pyright: ignore[reportPrivateUsage]
    assert lifecycle.request_stop() is not None
    assert lifecycle._card_edits.stopping is first  # pyright: ignore[reportPrivateUsage]
    await _settled(lifecycle)
    assert len(sender.sent) == 2, "one Stopping edit"


async def test_the_closing_notice_waits_for_a_slow_stopping_edit() -> None:
    sender = GatedSender(held=1)
    lifecycle = await _posted(sender)
    lifecycle.request_stop()
    await sender.entered.wait()  # The Stopping edit is out and unanswered.

    closing = asyncio.create_task(lifecycle.on_terminal_success(TurnState()))
    await asyncio.sleep(0.05)
    assert len(sender.sent) == 2 and not closing.done(), "Stopped. does not overtake it"
    sender.release.set()
    await closing
    assert len(sender.sent) == 3
    assert not _has_stop_button(sender, 1) and "Stopped." in _card_json(sender, -1)


async def test_a_failed_close_waits_for_a_slow_stopping_edit_too() -> None:
    sender = GatedSender(held=1)
    lifecycle = await _posted(sender)
    lifecycle.request_stop()
    await sender.entered.wait()
    closing = asyncio.create_task(lifecycle.close_with_notice("Something went wrong."))
    await asyncio.sleep(0.05)
    assert len(sender.sent) == 2 and not closing.done()
    sender.release.set()
    await closing
    assert "Something went wrong." in _card_json(sender, -1) and len(sender.sent) == 3


async def test_a_render_in_flight_at_the_click_lands_before_the_stopping_edit() -> None:
    sender, clock = GatedSender(held=1), Clock()
    lifecycle = await _posted(sender, clock)
    clock.now += 5
    render = asyncio.create_task(lifecycle.on_render(TurnState()))
    await sender.entered.wait()  # A "Working…" edit is out and unanswered.
    lifecycle.request_stop()
    await asyncio.sleep(0.05)
    assert len(sender.sent) == 2, "the Stopping edit waits its turn"
    sender.release.set()
    await render
    await _settled(lifecycle)
    assert len(sender.sent) == 3 and not _has_stop_button(sender, -1), "Stopping is the last word"


async def test_a_stopping_edit_that_failed_does_not_break_the_closing_notice() -> None:
    sender, clock = FakeSender(fail_on={1}), Clock()
    lifecycle = await _posted(sender, clock)
    lifecycle.request_stop()
    await _settled(lifecycle)
    clock.now += 16
    await lifecycle.on_terminal_success(TurnState())
    closing = _card_body(sender, -1)
    assert closing[0]["text"].startswith("Stopped."), "the terminal edit is the last word"
    assert closing[-1]["text"] == "16s", "the real time from the start until it stopped"
    sent = len(sender.sent)
    await lifecycle.on_render(TurnState())
    assert len(sender.sent) == sent, "a render after the close sends nothing"


async def test_a_stop_click_after_the_turn_ended_sends_nothing() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(TurnState())
    assert lifecycle.request_stop() is None and not lifecycle.stopping
    assert lifecycle._card_edits.stopping is None  # pyright: ignore[reportPrivateUsage]

    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.close_with_notice("Something went wrong.")
    assert lifecycle.request_stop() is None
    assert len(sender.sent) == 2, "only the closing edit"


async def test_a_close_that_wins_the_race_leaves_no_stopping_edit() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    lifecycle.request_stop()
    await lifecycle.close_with_notice("Stopped.")  # The edit task has not run yet.
    assert len(sender.sent) == 2 and "Stopped." in _card_json(sender, -1), "skipped, not late"


async def test_recovery_attempt_stays_stopping_and_follows_the_old_attempts_edit() -> None:
    sender = GatedSender(held=1)
    old = await _posted(sender)
    old.request_stop()
    await sender.entered.wait()
    adopted = TeamsTurnLifecycle(
        sender=sender,
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        cancel_key="key-1",
        clock=Clock(),
        adopt_message_id=old.message_id,
    )
    adopted.inherit_stop(old)
    assert adopted.stopping
    closing = asyncio.create_task(adopted.on_terminal_success(TurnState()))
    await asyncio.sleep(0.05)
    assert len(sender.sent) == 2 and not closing.done(), "waits behind the old attempt's edit"
    sender.release.set()
    await closing
    assert "Stopped." in _card_json(sender, -1), "the new attempt's notice is last"


def _adopt(old: TeamsTurnLifecycle, sender: FakeSender) -> TeamsTurnLifecycle:
    adopted = TeamsTurnLifecycle(
        sender=sender,
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        cancel_key="key-1",
        clock=Clock(),
        adopt_message_id=old.message_id,
    )
    adopted.inherit_stop(old)
    return adopted


async def test_a_stop_on_the_recovery_attempt_after_the_old_attempt_closed_sends_nothing() -> None:
    sender = FakeSender()
    old = await _posted(sender)
    adopted = _adopt(old, sender)
    await old.close_with_notice("Took too long.")  # the run closes the original attempt
    assert adopted.request_stop() is None, "the turn has ended on the card"
    assert len(sender.sent) == 2 and "Took too long." in _card_json(sender, -1)


async def test_the_old_attempts_close_waits_for_a_stop_clicked_on_the_recovery_attempt() -> None:
    sender = GatedSender(held=1)
    old = await _posted(sender)
    adopted = _adopt(old, sender)
    adopted.request_stop()
    await sender.entered.wait()
    closing = asyncio.create_task(old.close_with_notice("Took too long."))
    await asyncio.sleep(0.05)
    assert len(sender.sent) == 2 and not closing.done(), "waits behind the stopping edit"
    sender.release.set()
    await closing
    assert "Took too long." in _card_json(sender, -1), "the closing notice is last"


def _agent_message(event_id: str, text: str) -> Any:
    return BetaManagedAgentsAgentMessageEvent(
        id=event_id,
        type="agent.message",
        processed_at=datetime(2026, 1, 1, tzinfo=UTC),
        content=[BetaManagedAgentsTextBlock(type="text", text=text)],
    )


async def test_status_lines_show_as_a_running_log_on_the_card() -> None:
    sender, clock = FakeSender(), Clock()
    lifecycle = await _posted(sender, clock)
    for number in range(1, 5):
        await lifecycle.on_sse_event(_agent_message(f"evt_{number}", f"🔍 step {number}"))
    clock.now += 65
    await lifecycle.on_render(TurnState())

    body = _card_body(sender, -1)
    draft = next(block for block in body if block.get("isSubtle") and block.get("size") != "Small")
    assert draft["text"] == "🔍 step 2\n\n🔍 step 3\n\n🔍 step 4", "last three, one paragraph each"


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

    headline, details, tools, draft, elapsed, _actions = _card_body(sender, -1)
    assert headline["text"] == "**Working on it…**"
    assert (elapsed["text"], elapsed["isSubtle"]) == ("1m 5s", True), "elapsed time, quiet"
    assert details["text"] == "Details"
    assert tools["text"] == "✔️ Read a file\n\n🖋️ Running a command", "one paragraph per line"
    assert tools["fontType"] == "Monospace", "a TextBlock draws no code fence"
    assert (draft["text"], draft["isSubtle"]) == ("Checking the files.", True), "draft on one line"

    clock.now += 5
    finished = dataclasses.replace(bash, status="complete")
    await lifecycle.on_render(TurnState(content=[read, finished], finished_tool_ids=("t1", "t2")))
    assert _card_body(sender, -1)[0]["text"] == "**Working on it…**"


async def test_answer_replaces_the_card_and_the_controls_follow_it() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    lifecycle.answer_prefix = "Picked up where we left off."
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    answer, controls = sender.activities[1:]
    assert answer.id == "m-1" and not answer.attachments and not _rated(answer)
    assert answer.text == "Picked up where we left off.\n\nThe posterior mean is 3.", (
        "the answer alone, as on Discord and Slack"
    )
    assert controls.id is None and _rated(controls), "the 👍/👎 sit on the card below"
    assert _card_text(sender, -1) == "0s", "with nothing priced the summary line is the time"
    assert lifecycle.answer_prefix_applied
    assert lifecycle.card_closed and lifecycle.final_message_id == "m-1"


class _Sessions:
    """A sessionmaker stand-in: `balance_footer` is patched, so no query runs."""

    def __call__(self) -> contextlib.AbstractAsyncContextManager[object]:
        @contextlib.asynccontextmanager
        async def session() -> AsyncIterator[object]:
            yield object()

        return session()


_USAGE = UsageTotals(
    input_tokens=1000,
    cache_creation_input_tokens=500,
    cache_read_input_tokens=2000,
    output_tokens=300,
)


def _debited(model_id: str, markup: Decimal) -> str | None:
    t = _USAGE
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=t.input_tokens,
        cache_creation_input_tokens=t.cache_creation_input_tokens,
        cache_read_input_tokens=t.cache_read_input_tokens,
        output_tokens=t.output_tokens,
        speed="standard",
    )
    cost = cost_of(usage, MODEL_PRICING[model_id])
    assert cost is not None
    return format_cost(float(debit_amount(cost, markup=markup)))


async def test_the_summary_line_names_the_agent_time_debit_and_money_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack's line: `used` is the debit, markup included, so it agrees with `left`."""
    asked: list[dict[str, Any]] = []

    async def footer(session: object, **kw: Any) -> str:
        asked.append(kw)
        return "$41.20 left"

    monkeypatch.setattr(lifecycle_module, "balance_footer", footer)
    sender, clock, tenant_id = FakeSender(), Clock(), uuid.uuid4()
    lifecycle = await _posted(
        sender,
        clock,
        tenant_id=tenant_id,
        agent_name="Ada",
        model_id="claude-opus-5-5",
        markup=Decimal("1.1"),
        sessionmaker=_Sessions(),
        budget_channel_id="19:budget",
    )
    clock.now += 12
    state = dataclasses.replace(_answer("Done."), usage_totals=_USAGE)
    await lifecycle.on_terminal_success(state)

    expected = format_summary(
        agent_name="Ada",
        elapsed_seconds=12,
        cost=_debited("claude-opus-5-5", Decimal("1.1")),
        left="$41.20 left",
    )
    assert _card_text(sender, -1) == expected
    assert asked[0]["tenant_id"] == tenant_id and asked[0]["platform"] == "teams"
    assert asked[0]["budget_channel_id"] == "19:budget", "a channel budget's remainder wins"


@pytest.mark.parametrize(
    "error", [OperationalError("select", {}, OSError("refused")), RuntimeError("anything")]
)
async def test_the_summary_line_leaves_out_a_name_shown_above_and_money_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    async def unreadable(session: object, **kw: Any) -> str:
        raise error

    monkeypatch.setattr(lifecycle_module, "balance_footer", unreadable)
    kw: dict[str, Any] = {
        "tenant_id": uuid.uuid4(),
        "agent_name": "Ada",
        "agent_name_prefix": "Ada",
        "sessionmaker": _Sessions(),
    }
    sender = FakeSender()
    await (await _posted(sender, **kw)).on_terminal_success(_answer("Done."))
    assert _card_text(sender, -1) == "0s", "identity named the agent; the balance read failed"
    assert _rated(sender.activities[-1]), "the controls still land"

    sender = FakeSender()
    tool = ToolUseBlock(kind="tool_use", id="t1", type="agent.tool_use", name="bash", input={})
    await (await _posted(sender, **kw)).on_terminal_success(TurnState(content=[tool]))
    _done, summary, _votes = _card_body(sender, -1)
    assert summary["text"] == "Ada\u2003\u20030s", "no answer above names the agent here"


async def test_a_failed_turn_card_carries_the_summary_line() -> None:
    sender, clock = FakeSender(), Clock()
    lifecycle = await _posted(sender, clock, agent_name="Ada")
    clock.now += 12
    error = TurnError(kind="upstream", message="overloaded")
    await lifecycle.on_terminal_failure(TurnState(error=error), error)

    summary = _card_body(sender, -1)[-1]
    assert summary["text"] == "Ada\u2003\u200312s" and summary["isSubtle"], "as on Slack"
    assert not _rated(sender.activities[-1]), "a failure is not an answer to rate"


async def test_agent_name_prefix_is_only_on_first_answer_chunk() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender, agent_name_prefix="Ada")
    await lifecycle.on_terminal_success(_answer("One.\n\n" + "Long answer. " * 600))
    answers = [activity.text or "" for activity in sender.activities[1:]]
    assert len(answers) > 1
    assert answers[0].startswith("**Ada**\n\n")
    assert all("**Ada**" not in chunk for chunk in answers[1:])


async def test_builtin_answer_has_no_name_prefix() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("Done."))
    assert sender.activities[1].text == "Done."


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
    assert "reached its usage limit" in _card_json(sender, -1)
    assert {
        "event": "anthropic.spend_limit_reached",
        "log_level": "error",
        "tenant_id": str(tenant_id),
        "limit": "org_cap",
    } in logs


async def test_a_long_answer_overflows_into_new_messages_then_the_controls() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    paragraph = "word " * 1000
    await lifecycle.on_terminal_success(_answer("\n\n".join([paragraph] * 4)))

    *chunks, controls = sender.activities[1:]
    assert len(chunks) >= 2
    assert chunks[0].id == "m-1" and all(c.id is None for c in chunks[1:])
    assert not any(_rated(c) or c.attachments for c in chunks), "the answer is text alone"
    assert _rated(controls), "the 👍/👎 follow the last part"
    assert lifecycle.final_message_id == f"m-{len(sender.sent) - 1}", "the answer's last part"

    assert await lifecycle.append_to_answer("- [a](u)", [_image("a.png")])
    edit = sender.activities[-1]
    assert edit.id == lifecycle.final_message_id, "links and previews go in the last part only"
    assert (edit.text or "").startswith(chunks[-1].text or "-") and _names(edit) == ["a.png"]
    assert sender.activities[1:-1] == [*chunks, controls], "nothing is posted below the controls"


async def test_a_failed_answer_post_collapses_the_card_and_leaves_no_watermark() -> None:
    sender = FakeSender(fail_on={1})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("never seen"))

    assert sender.activities[-1].id == "m-1"
    assert "Something went wrong." in _card_json(sender, -1)
    assert lifecycle.card_closed and lifecycle.final_message_id is None


async def test_direct_chat_failure_says_send_a_message() -> None:
    sender = FakeSender(fail_on={1})
    lifecycle = await _posted(sender, direct_chat=True)
    await lifecycle.on_terminal_success(_answer("never seen"))

    failure = _card_json(sender, -1)
    assert "Send a message to try again." in failure
    assert "Mention me" not in failure


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

    assert [a.id for a in sender.activities[1:]] == ["m-1", "m-1", None], "the edit is resent"
    assert "The posterior mean is 3." in _card_json(sender, -2)
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
    assert "Stopped." in _card_json(sender, -1) and lifecycle.card_closed

    sender = FakeSender(timeout_on={1, 2})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(TurnState())
    assert sender.activities[-1].id == "m-1"
    assert "Something went wrong." in _card_json(sender, -1), "no answer is claimed"
    assert lifecycle.card_closed


async def test_a_completion_ping_posts_the_answer_mentioning_the_asker_then_closes_the_card() -> (
    None
):
    sender = FakeSender()
    asker = Account(id="aad-1", name="Ada")
    lifecycle = await _posted(sender, completion_ping=True, requester=asker)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    answer, closed, controls = sender.activities[1:]
    assert closed.id == "m-1" and card.ANSWERED_BELOW in closed.model_dump_json()
    assert controls.id is None and _rated(controls), "the controls follow the answer"
    assert answer.id is None and answer.text == "<at>Ada</at>\n\nThe posterior mean is 3."
    assert [e.mentioned.id for e in answer.entities or [] if isinstance(e, MentionEntity)] == [
        "aad-1"
    ]
    assert lifecycle.card_closed and lifecycle.final_message_id == "m-2"

    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    edited = sender.activities[-1]
    assert edited.id == "m-2" and edited.text is not None
    assert edited.text.startswith("<at>Ada</at>\n\nI lost the workspace."), "edits keep the ping"


async def test_without_a_requester_a_ping_still_posts_fresh() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender, completion_ping=True)
    await lifecycle.on_terminal_success(_answer("Done."))
    assert [a.id for a in sender.activities] == [None, None, "m-1", None], "the 1:1 chat gets no @"
    assert sender.activities[1].text == "Done."


async def test_a_ping_whose_card_cannot_be_retired_still_delivers_the_answer() -> None:
    sender = FakeSender(fail_on={2})
    lifecycle = await _posted(sender, completion_ping=True)
    await lifecycle.on_terminal_success(_answer("Done."))
    assert sender.activities[1].text == "Done." and lifecycle.final_message_id == "m-2"
    assert lifecycle.card_closed, "an answered turn is never swept as interrupted"


async def test_a_ping_whose_answer_fails_collapses_the_card_to_the_failure() -> None:
    sender = FakeSender(fail_on={1})
    lifecycle = await _posted(sender, completion_ping=True)
    await lifecycle.on_terminal_success(_answer("Done."))
    assert sender.activities[-1].id == "m-1" and card.ANSWERED_BELOW not in _card_json(sender, -1)
    assert lifecycle.final_message_id is None, "no watermark past an answer nobody saw"


@pytest.mark.parametrize(
    ("ping", "state", "expected"),
    [
        (True, _answer("Done."), ["accepted", "done"]),
        (True, TurnState(content=[_TOOL]), ["accepted", "done"]),
        (True, TurnState(), ["accepted"]),
        (False, _answer("Done."), []),
    ],
    ids=["answered", "tool-only", "cancelled", "no-ping"],
)
async def test_a_ping_reacts_on_the_askers_message_as_on_discord(
    ping: bool, state: TurnState, expected: list[str]
) -> None:
    phases: list[str] = []
    lifecycle = await _posted(FakeSender(), completion_ping=ping, acknowledge=phases.append)
    await lifecycle.on_acknowledgment("accepted")
    await lifecycle.on_terminal_success(state)
    await lifecycle.on_acknowledgment("done")
    assert phases == expected, "👀 on acceptance with a ping, ✅ only once something was answered"


async def test_a_failed_answer_or_an_unprompted_turn_earns_no_check() -> None:
    phases: list[str] = []
    lifecycle = await _posted(
        FakeSender(fail_on={1}), completion_ping=True, acknowledge=phases.append
    )
    await lifecycle.on_terminal_success(_answer("Done."))
    await lifecycle.on_acknowledgment("done")
    unprompted = TeamsTurnLifecycle(
        sender=FakeSender(),
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        cancel_key="key-1",
        unprompted=True,
        completion_ping=True,
        acknowledge=phases.append,
    )
    await unprompted.on_acknowledgment("accepted")
    assert phases == [], "no ✅ for an answer nobody saw, no reaction in a thread nobody asked in"


async def test_a_late_notice_is_edited_in_above_the_answer() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    final = sender.activities[-1]
    assert final.id == "m-1" and final.text is not None
    assert final.text == "I lost the workspace.\n\nThe posterior mean is 3."
    assert not _rated(final) and not final.attachments, "the edit stays text, as Teams needs"


async def test_a_late_notice_whose_edit_timed_out_counts_as_shown() -> None:
    """A timed-out edit may have landed; sending the notice as well could show it twice."""
    sender = FakeSender(timeout_on={3, 4})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    assert [a.id for a in sender.activities[3:]] == ["m-1", "m-1"], "edited, then retried once"


async def test_a_late_notice_on_a_turn_without_an_answer_is_edited_into_its_card() -> None:
    """No answer on screen: the turn's own closing card takes the notice, nothing new posts."""
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(TurnState(content=[]))
    posted = len(sender.activities)

    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    edit = sender.activities[-1]
    assert len(sender.activities) == posted + 1 and edit.id == "m-1", "an edit of the card"
    assert "I lost the workspace." in json.dumps(edit.model_dump(mode="json"))


async def test_file_links_are_edited_in_below_a_prefixed_single_message_answer() -> None:
    """Prefix and links land on the same message, each edit keeping the other."""
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    assert await lifecycle.append_to_answer("- [a](u)")
    final = sender.activities[-1]
    assert final.id == "m-1", "the answer is edited, nothing new is posted"
    assert final.text == ("I lost the workspace.\n\nThe posterior mean is 3.\n\n- [a](u)")
    assert not _rated(final) and not final.attachments


async def test_file_links_that_would_overflow_the_last_message_are_refused() -> None:
    """Over the Teams limit the caller sends the links as their own message instead."""
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("x" * (card.TEAMS_LIMIT - 5)))

    assert not await lifecycle.append_to_answer("- [a](u)")
    assert len(sender.sent) == 3, "no edit was tried"


def _image(name: str) -> Attachment:
    return Attachment(content_type="image/png", content_url="data:image/png;base64,AA==", name=name)


def _names(activity: MessageActivityInput) -> list[str]:
    """The previews shown in an answer's text, by alt text; it carries no attachment."""
    assert not activity.attachments, "Teams refuses text beside an attachment"
    return re.findall(r"!\[((?:\\.|[^\]])*)\]\(data:image/", activity.text or "")


async def test_previews_ride_the_links_edit_and_later_edits_keep_them() -> None:
    """One edit adds links and images; another adds more; a notice above sends them again."""
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    assert await lifecycle.append_to_answer("- [a](u)", [_image("a.png")])
    assert await lifecycle.append_to_answer(attachments=[_image("deck.pptx")])
    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    links, thumbnails, notice = sender.activities[-3:]
    assert {links.id, thumbnails.id, notice.id} == {"m-1"}, "each one an edit of the answer"
    assert links.text == (
        "The posterior mean is 3.\n\n- [a](u)\n\n![a.png](data:image/png;base64,AA==)"
    ), "the image follows the links, in the same text"
    assert links.text_format == "markdown", "Graph reads back a markdown message's images"
    assert _names(thumbnails) == ["a.png", "deck.pptx"], "added after the one already there"
    assert (thumbnails.text or "").startswith(links.text or "-"), "no text added"
    assert notice.text and notice.text.startswith("I lost the workspace.")
    assert _names(notice) == ["a.png", "deck.pptx"], "an edit never drops a shown preview"


async def test_previews_the_answer_cannot_take_are_refused() -> None:
    """A notice card, an overflow or a refused edit: the caller sends the previews instead."""
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(TurnState(content=[_TOOL]))
    posted = len(sender.sent)
    assert not await lifecycle.append_to_answer(attachments=[_image("a.png")]), "a notice card"
    assert len(sender.sent) == posted, "no edit was tried"
    assert await lifecycle.append_to_answer("- [a](u)"), "links still fit"

    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("x" * (card.TEAMS_LIMIT - 5)))
    assert not await lifecycle.append_to_answer("Saved:", [_image("a.png")]), "too long"

    sender = FakeSender(refuse_on={3})
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))
    assert not await lifecycle.append_to_answer(attachments=[_image("a.png")]), "a 413"
    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    assert _names(sender.activities[-1]) == [], "a refused preview is not sent again"


async def test_previews_stay_out_of_an_answer_with_math() -> None:
    """Plain markdown shows math raw, so the previews go in a message of their own."""
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The mean is $\\mu = 3$."))
    edits = len(sender.sent)

    assert not await lifecycle.append_to_answer(attachments=[_image("a.png")]), "math"
    assert len(sender.sent) == edits, "no edit was tried"
    assert await lifecycle.append_to_answer("- [a](u)"), "links still fit"
    assert sender.activities[-1].text_format == "extendedmarkdown", "the math still renders"


async def test_a_pinged_answer_keeps_its_mention_beside_its_previews() -> None:
    """A ping is plain markdown already, so math does not hold previews back; the edit keeps it."""
    sender = FakeSender()
    asker = Account(id="aad-1", name="Ada")
    lifecycle = await _posted(sender, completion_ping=True, requester=asker)
    await lifecycle.on_terminal_success(_answer("The mean is $\\mu = 3$."))

    assert await lifecycle.append_to_answer(attachments=[_image("a.png")])
    edited = sender.activities[-1]
    assert edited.id == "m-2" and edited.text_format == "markdown", "an edit of the answer"
    assert edited.text == (
        "<at>Ada</at>\n\nThe mean is $\\mu = 3$.\n\n![a.png](data:image/png;base64,AA==)"
    ), "the image follows the mention and the answer"
    assert [e.mentioned.id for e in edited.entities or [] if isinstance(e, MentionEntity)] == [
        "aad-1"
    ], "the ping survives the edit"


def test_preview_alt_text_cannot_break_out_of_its_image() -> None:
    """A file name with brackets or markup stays the image's alt text."""
    image = card.inline_image(_image("a]![x](https://example.com/y) *b*\n.png"))
    assert image == (
        r"![a\]!\[x\](https://example.com/y) \*b\* .png](data:image/png;base64,AA==)"
    ), "brackets and markup are escaped, the newline folded"


async def test_an_answer_holds_no_more_previews_than_one_preview_message() -> None:
    """Thumbnails that would take the answer past MAX_PREVIEWS images are refused whole."""
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))
    charts = [_image(f"c{n}.png") for n in range(MAX_PREVIEWS - 1)]
    assert await lifecycle.append_to_answer(attachments=charts)
    edits = len(sender.sent)

    assert not await lifecycle.append_to_answer(attachments=[_image("a.pdf"), _image("b.pdf")])
    assert len(sender.sent) == edits, "no edit was tried"
    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    assert _names(sender.activities[-1]) == [a.name for a in charts], "the charts stay, alone"


async def test_no_answer_reads_as_cancelled_or_done() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    await lifecycle.on_terminal_success(TurnState())
    assert "Stopped." in _card_json(sender, -1)

    sender = FakeSender()
    lifecycle = await _posted(sender)
    tool = ToolUseBlock(kind="tool_use", id="t1", type="agent.tool_use", name="bash", input={})
    await lifecycle.on_terminal_success(TurnState(content=[tool]))
    assert _card_text(sender, -1) == card.TOOLS_DONE_NOTICE, "a tool-only turn closes plainly"


def _card_text(sender: FakeSender, index: int) -> str:
    return json.loads(_card_json(sender, index))["attachments"][0]["content"]["body"][0]["text"]


def _rated(activity: MessageActivityInput) -> bool:
    """The 👍/👎 buttons, never Teams' own feedback loop."""
    data = activity.channel_data
    assert data is None or data.feedback_loop is None
    rendered = activity.model_dump_json(by_alias=True)
    return card.VOTE_UP_DIALOG in rendered and card.VOTE_DOWN_DIALOG in rendered


def _asks(sender: FakeSender, index: int) -> bool:
    return card.ASK_HUMAN_DIALOG in _card_json(sender, index)


@pytest.mark.parametrize("ask_human", [True, False])
async def test_the_answer_stays_text_and_its_controls_follow_in_a_card(ask_human: bool) -> None:
    """As on Slack, the summary, Ask a human and 👍/👎 sit under the final answer. In a card
    of their own: Teams refuses an edit carrying both text and a card, so every answer failed."""
    sender = FakeSender()
    lifecycle = await _posted(sender, ask_human=ask_human)
    await lifecycle.on_terminal_success(_answer("x" * (card.TEAMS_LIMIT + 10)))

    first, last = [activity for activity in sender.activities if activity.text]
    assert not any(_rated(a) or a.attachments for a in (first, last)), "the answer is text alone"
    assert last.text and last.text.startswith("x"), "the answer stays markdown text"
    assert _rated(sender.activities[-1]), "the 👍/👎 open daimon's own form"
    assert _asks(sender, -1) is ask_human, "Ask a human only when support is on"
    assert await lifecycle.append_to_answer("- [a](u)")
    edit = sender.activities[-1]
    assert edit.text and not _rated(edit) and not edit.attachments, "an edit stays text"


async def test_a_lost_controls_card_leaves_the_answer_as_delivered() -> None:
    """The answer landed: controls that fail to post are no cut-short answer."""
    sender = FakeSender(fail_on={2})
    lifecycle = await _posted(sender, ask_human=True)
    await lifecycle.on_terminal_success(_answer("hello"))

    assert len(sender.activities) == 3, "no notice follows the lost card"
    assert _asks(sender, -1), "the third send was the controls"
    assert lifecycle.final_message_id == sender.activities[1].id, "the answer counts as seen"


def _wrote(command: str, *, done: bool = True) -> TurnState:
    bash = ToolUseBlock(
        kind="tool_use", id="t1", type="agent.tool_use", name="bash", input={"command": command}
    )
    finished = ("t1",) if done else ()
    return TurnState(
        content=[bash, TextBlock(kind="text", text="Made it.")], finished_tool_ids=finished
    )


async def test_held_controls_wait_for_their_release_and_post_once() -> None:
    """A turn that wrote to the outputs folder: the controls follow the sweep's files."""
    sender = FakeSender()
    lifecycle = await _posted(sender, hold_controls=True)
    await lifecycle.on_terminal_success(_wrote("python plot.py > /mnt/session/outputs/a.png"))
    assert len(sender.activities) == 2 and not _rated(sender.activities[-1]), "answer only"

    await lifecycle.release_controls()
    await lifecycle.release_controls()
    assert len(sender.activities) == 3 and _rated(sender.activities[-1]), "then once, below"


async def test_a_turn_that_wrote_no_outputs_posts_its_controls_at_once() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender, hold_controls=True)
    await lifecycle.on_terminal_success(_wrote("ls /tmp"))
    assert _rated(sender.activities[-1]), "nothing to wait for"
    await lifecycle.release_controls()
    assert len(sender.activities) == 3, "the release posts nothing more"


async def test_outputs_quiet_time_runs_from_the_last_finished_tool_call() -> None:
    sender, clock = FakeSender(), Clock()
    lifecycle = await _posted(sender, clock)
    assert lifecycle.outputs_quiet_s() is None, "no tool call ended"
    await lifecycle.on_render(_wrote("ls"))
    clock.now += 3
    assert lifecycle.outputs_quiet_s() == 3.0
    await lifecycle.on_render(_wrote("ls"))
    assert lifecycle.outputs_quiet_s() == 3.0, "the same call ending again restarts nothing"
    await lifecycle.on_render(_wrote("ls", done=False))
    assert lifecycle.outputs_quiet_s() is None, "an open call may still be writing"


async def test_with_support_on_a_one_part_answer_and_its_edits_are_ones_teams_takes() -> None:
    """Production's shape: one part, edited in place, then amended by later notices."""
    sender = FakeSender()
    lifecycle = await _posted(sender, ask_human=True)
    await lifecycle.on_terminal_success(_answer("The posterior mean is 3."))

    answer, controls = sender.activities[1:]
    assert answer.id == "m-1" and answer.text == "The posterior mean is 3.", "the card became it"
    assert controls.id is None and _asks(sender, -1), "the button follows in a post of its own"
    assert await lifecycle.prepend_revealed_answer("I lost the workspace."), "a notice fits above"
    assert await lifecycle.append_to_answer("- [a](u)"), "links fit below"
    assert all(not a.attachments for a in sender.activities[-2:]), "both edits stay text"


async def test_a_completion_ping_with_support_on_posts_the_button_below_the_answer() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender, completion_ping=True, ask_human=True)
    await lifecycle.on_terminal_success(_answer("Done."))

    answer, closed, button = sender.activities[1:]
    assert answer.id is None and answer.text == "Done." and not answer.attachments
    assert closed.id == "m-1" and card.ANSWERED_BELOW in closed.model_dump_json(), "card retired"
    assert button.id is None and _asks(sender, 3), "then the button follows the answer"


async def test_a_tool_only_turn_carries_the_controls_and_a_cancelled_one_none() -> None:
    """#428's rule: a finished tool-only turn is an answer; a cancelled turn is not."""
    sender = FakeSender()
    lifecycle = await _posted(sender, ask_human=True)
    tool = ToolUseBlock(kind="tool_use", id="t1", type="agent.tool_use", name="bash", input={})
    await lifecycle.on_terminal_success(TurnState(content=[tool]))
    assert _rated(sender.activities[-1]) and _asks(sender, -1), "✅ Done can be rated"
    assert len(sender.activities) == 2, "the Done card carries them itself"
    assert _card_body(sender, -1)[1]["text"] == "0s", "and the summary line"
    assert await lifecycle.prepend_revealed_answer("I lost the workspace.")
    assert _rated(sender.activities[-1]) and _asks(sender, -1), "an edit keeps the controls"

    sender = FakeSender()
    lifecycle = await _posted(sender, ask_human=True)
    await lifecycle.on_terminal_success(TurnState())
    assert not _rated(sender.activities[-1]) and not _asks(sender, -1), "cancelled: no controls"
    assert _card_body(sender, -1)[1]["text"] == "0s", "but the summary line, as on Slack"


async def test_failure_closes_the_card_with_the_termination_notice_once() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender)
    error = TurnError(kind="upstream", message="overloaded")
    await lifecycle.on_terminal_failure(TurnState(error=error), error)
    await lifecycle.close_with_notice("second notice is ignored")

    assert len(sender.sent) == 2, "the card closes once"
    text = _card_text(sender, 1)
    assert text.startswith("Daimon's AI service failed on this reply.")
    assert "Ref RID-1" in _card_json(sender, 1)
    assert lifecycle.card_closed


async def test_direct_chat_terminal_notice_asks_for_a_message() -> None:
    sender = FakeSender()
    lifecycle = await _posted(sender, direct_chat=True)
    error = TurnError(kind="upstream", message="overloaded")
    await lifecycle.on_terminal_failure(TurnState(error=error), error)
    assert "Send your message again." in _card_json(sender, 1)
    assert "@mention" not in _card_json(sender, 1)


async def test_a_notice_that_fails_to_build_falls_back_to_the_raw_error() -> None:
    def broken() -> str:
        raise RuntimeError("no id")

    sender = FakeSender()
    lifecycle = await _posted(sender, request_id=broken)
    error = TurnError(kind="upstream", message="overloaded")
    await lifecycle.on_terminal_failure(TurnState(error=error), error)

    assert _card_text(sender, 1) == "Something went wrong.\n\n@mention Daimon to try again."


def test_an_oversized_notice_fits_the_teams_limit_and_keeps_the_request_id() -> None:
    notice = render_termination_notice(TerminationReason.UPSTREAM, request_id="rid-1")
    assert notice is not None
    huge = dataclasses.replace(notice, title="x" * 10_000)

    text = card.termination_text(huge)

    assert len(text) <= card.TEAMS_LIMIT, "Teams rejects an oversized message"
    assert text.endswith("…\n\nRef RID-1"), "clipped before the tail"


def test_teams_draws_work_and_ref_as_small_text() -> None:
    notice = render_termination_notice(
        TerminationReason.CONNECTION_LOST,
        state=TurnState(
            content=[
                ToolUseBlock(
                    kind="tool_use",
                    id="tu_1",
                    type="agent.tool_use",
                    name="fetch",
                    input={},
                )
            ]
        ),
        request_id="01A7C3QX2",
    )
    assert notice is not None
    rendered = card.notice_card(card.termination_text(notice)).model_dump(
        mode="json", by_alias=True
    )
    body = rendered["attachments"][0]["content"]["body"]
    assert body[0]["text"].startswith("Lost the connection. Daimon may still be working.")
    assert body[1]["size"] == "Small" and body[1]["isSubtle"]
    assert body[2]["text"] == "Ref 7C3QX2"
    assert body[2]["size"] == "Small" and body[2]["isSubtle"]


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
    answer, controls = sender.activities
    assert answer.id is None and answer.text == "Thursdays.", "posted, not an edit"
    assert _rated(controls), "its controls follow, as on any answer"
    assert lifecycle.final_message_id == "m-1", "the watermark can move past it"


async def test_an_unprompted_turn_without_an_answer_or_with_a_failure_stays_silent() -> None:
    sender = FakeSender()
    tool = ToolUseBlock(kind="tool_use", id="t1", type="agent.tool_use", name="bash", input={})
    await _unprompted(sender).on_terminal_success(TurnState(content=[tool]))
    error = TurnError(kind="upstream", message="overloaded")
    await _unprompted(sender).on_terminal_failure(TurnState(error=error), error)
    await _unprompted(sender).close_with_notice("Sorry, something went wrong.")
    assert sender.sent == [], "no tool trail, failure card or notice in a thread nobody asked in"


async def test_unchanged_running_state_rerendered_moves_the_elapsed_time() -> None:
    """The driver re-renders an unchanged state on every tick during a long
    tool call; each debounced edit of the same card shows a later elapsed time."""
    sender, clock = FakeSender(), Clock()
    lifecycle = await _posted(sender, clock)
    running = TurnState(
        content=[
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={})
        ]
    )

    for elapsed in (1, 12, 13, 24):
        clock.now = 100.0 + elapsed
        await lifecycle.on_render(running)

    assert len(sender.activities) == 3
    assert all(activity.id == "m-1" for activity in sender.activities[1:])
    assert [_card_body(sender, i)[-2]["text"] for i in (1, 2)] == ["12s", "24s"]


async def test_nothing_posted_names_another_app() -> None:
    sender, clock = FakeSender(), Clock()
    inner = await _posted(sender, clock)
    lc = OtherAppsScrubbedLifecycle(inner, "teams")
    await lc.on_sse_event(
        BetaManagedAgentsAgentMessageEvent(
            id="e1",
            type="agent.message",
            content=[BetaManagedAgentsTextBlock(type="text", text="Looking in Discord or Slack")],
            processed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    tool = ToolUseBlock(
        kind="tool_use", id="t1", type="agent.tool_use", name="discord_read_messages", input={}
    )
    clock.now += 10
    await lc.on_render(TurnState(content=[tool]))
    live = _card_json(sender, -1)
    assert "Looking in another app" in live
    assert "Discord" not in live and "Slack" not in live

    reply = "Teams is right here. Never upload customer data to Discord or Slack."
    await lc.on_terminal_success(_answer(reply))
    answer = sender.activities[-2].text
    assert answer == "Teams is right here. Never upload customer data to another app."
