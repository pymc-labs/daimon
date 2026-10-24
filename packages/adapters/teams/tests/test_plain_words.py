"""Approved Teams copy and card spacing."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from daimon.adapters.teams import feedback, memory, wizard
from daimon.adapters.teams.billing_panel import redeemed_card_text
from daimon.adapters.teams.help import help_card
from daimon.adapters.teams.private_replies import NOT_YET_PRIVATE
from daimon.core.promo_credit import PromoRedeemed


def test_feedback_memory_and_wizard_errors() -> None:
    assert feedback.THANKS_VOTE == "Thanks."
    assert feedback.THANKS_TEXT == "Thanks for the feedback."
    assert feedback.NEEDS_ONE == "Pick a reason or write a few words."
    assert memory.KEPT_INSIDE == (
        "This channel's rules keep its agent's memory out of this chat.\n\n"
        "Ask the agent in that channel."
    )
    assert wizard._STALE == "This form just changed.\n\nCheck it and try again."  # pyright: ignore[reportPrivateUsage]


def test_help_keeps_registered_commands_and_spaces_the_last_block() -> None:
    card = help_card(["new", "setup", "support", "help"], bot="Daimon")
    data = card.model_dump(by_alias=True)
    facts = next(item["facts"] for item in data["body"] if item["type"] == "FactSet")
    assert [fact["title"] for fact in facts] == ["new", "setup", "support", "help"]
    assert [item["text"] for item in data["body"][-2:]] == ["💬 Or just ask", "Help me set up"]
    assert data["body"][-1]["spacing"] == "Medium"


def test_copy_shown_in_channels_and_group_chats_fits_both() -> None:
    """A group chat has no thread; a command there is a mention, as in a channel."""
    card = help_card(["help"], bot="Daimon", place="group").model_dump(by_alias=True)
    assert card["body"][1]["text"] == (
        "Only you can see this. @mention Daimon with one of these words."
    )
    assert "Mention Daimon here" in NOT_YET_PRIVATE.format(bot="Daimon")
    assert "thread" not in NOT_YET_PRIVATE


def test_timed_credit_has_the_approved_two_paragraph_reply() -> None:
    result = PromoRedeemed(
        promo_code_id=uuid.uuid4(),
        kind="timed",
        amount_usd=Decimal("25"),
        credit_starts_at=None,
        credit_ends_at=datetime(2026, 10, 12, 18, tzinfo=UTC),
        granted=True,
        balance_usd=Decimal("87.4"),
    )
    text = redeemed_card_text(result)
    assert text.startswith("🎟️ Added **$25.00** of credit.\n\nUsed before credit with no expiry. ")
    assert "Anything unused expires" in text
