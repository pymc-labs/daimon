"""`answer_message`: the text format each kind of answer chunk goes out in."""

from __future__ import annotations

from daimon.adapters.teams import card
from microsoft_teams.api import Account, MentionEntity


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
