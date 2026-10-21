"""`answer_message`: the text format each kind of answer chunk goes out in."""

from __future__ import annotations

from typing import Any

import pytest
from daimon.adapters.teams import card
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.state import ToolUseBlock, TurnState
from microsoft_teams.api import Account, MentionEntity
from sqlalchemy.ext.asyncio import async_sessionmaker


def test_answer_chunk_is_extended_markdown_with_currency_escaped() -> None:
    """A plain chunk renders math and tables; a price's `$` stays literal."""
    message = card.answer_message("Costs $5. Area: $\\pi r^2$.")
    assert message.text_format == "extendedmarkdown", "answers use Extended Markdown"
    assert message.text == "Costs \\$5. Area: $\\pi r^2$.", "currency escaped, math kept"


def test_answer_chunk_with_a_mention_stays_markdown() -> None:
    """A completion ping keeps plain markdown: Extended Markdown's `<at>` support is unknown."""
    requester = Account(id="00000000-0000-0000-0000-000000000001", name="Ada")
    message = card.answer_message("Costs $5.", mention=requester)
    assert message.text_format == "markdown", "a mention keeps the format known to ping"
    assert message.text == "<at>Ada</at>\n\nCosts $5.", "markdown needs no dollar escape"
    assert any(isinstance(e, MentionEntity) for e in message.entities or []), "mention entity"


def _live(state: card.CardState) -> dict[str, Any]:
    activity = card.status_card(state, now=165.0, cancel_key="k")
    return activity.model_dump(by_alias=True)["attachments"][0]["content"]


def test_the_stopping_card_says_so_has_no_stop_button_and_keeps_the_elapsed_time() -> None:
    working = _live(card.CardState(started_at=100.0))
    stopping = _live(card.on_stop(card.CardState(started_at=100.0)))
    assert "ActionSet" in str(working) and card.CANCEL_VERB in str(working)
    headline, elapsed = stopping["body"]
    assert headline["text"] == "**Stopping…**" and elapsed["text"] == "1m 5s"
    assert "ActionSet" not in str(stopping) and card.CANCEL_VERB not in str(stopping)
    assert stopping["fallbackText"] == card.STOPPING_HEADLINE


def test_stopping_survives_the_turns_later_activity() -> None:
    state = card.on_activity(card.on_stop(card.CardState(started_at=0.0)), TurnState())
    assert state.stopping


def _draft_text(state: card.CardState) -> str | None:
    body = _live(state)["body"]
    drafts = [block for block in body if block.get("isSubtle") and block.get("size") != "Small"]
    return drafts[0]["text"] if drafts else None


def test_a_card_with_no_draft_shows_no_draft_block() -> None:
    assert _draft_text(card.CardState(started_at=100.0)) is None


@pytest.mark.parametrize("count", [1, 3, 5])
def test_the_card_logs_the_last_three_draft_lines_one_paragraph_each(count: int) -> None:
    state = card.CardState(started_at=100.0)
    for number in range(1, count + 1):
        state = card.on_message(state, f"🔍 step {number}")
    shown = range(max(1, count - 2), count + 1)
    assert _draft_text(state) == "\n\n".join(f"🔍 step {n}" for n in shown)


def test_a_provider_preview_revises_the_latest_line_without_growing_the_log() -> None:
    state = card.on_message(card.on_message(card.CardState(started_at=100.0), "one"), "two")
    state = card.on_draft(state, "two, longer")
    assert _draft_text(state) == "one\n\ntwo, longer"


def test_a_draft_line_is_cut_to_120_characters() -> None:
    state = card.on_message(card.CardState(started_at=100.0), "z" * 400)
    assert _draft_text(state) == "z" * 120 + "…"


def _plan_turn() -> TurnState:
    steps = [{"step": "Load data", "status": "completed"}, {"step": "Fit", "status": "in_progress"}]
    plan = ToolUseBlock(
        kind="tool_use",
        id="tu_plan",
        type="agent.mcp_tool_use",
        name="update_plan",
        input={"steps": steps},
        mcp_server_name=DAIMON_SERVER_NAME,
        status="complete",
    )
    return TurnState(content=[plan])


@pytest.mark.parametrize("on", [True, False])
def test_the_plan_shows_as_its_own_block_only_with_progress_plans_on(on: bool) -> None:
    """With the switch on the update is a checklist under the headline, not a tool line."""
    observation = TurnObservation(async_sessionmaker(), None, "teams", progress_plans=on)
    with observation.activate():
        state = card.on_activity(card.CardState(started_at=100.0), _plan_turn())
    texts = [block["text"] for block in _live(state)["body"] if "text" in block]
    if on:
        assert texts[1] == "✔️ Load data\n\n▶️ Fit", "one paragraph per step after the headline"
        assert "Details" not in texts, "the plan update is not listed as a tool"
    else:
        assert not any("▶️" in text for text in texts), "off: no checklist"
        assert "Details" in texts, "off: the call is an ordinary tool line"
