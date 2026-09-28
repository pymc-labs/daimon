"""Content-bounded Teams presentation helpers.

Teams renders each streamed message from one card payload and caps that
payload well below the size an agent answer can reach, so every string this
package puts on the wire goes through ``bounded_text`` first — UTF-8 safe,
with a visible marker when clipping happened.
"""

from __future__ import annotations

from microsoft_teams.api import MessageActivityInput  # pyright: ignore[reportMissingTypeStubs]
from microsoft_teams.cards import AdaptiveCard  # pyright: ignore[reportMissingTypeStubs]
from microsoft_teams.cards import (  # pyright: ignore[reportMissingTypeStubs]
    TextBlock as TeamsTextBlock,
)

WORKING_MESSAGE = "Working on it…"
INTERRUPTED_MESSAGE = "This turn was interrupted by a restart."
FAILURE_MESSAGE = "⚠️ Something went wrong running this turn."
NO_ANSWER_MESSAGE = "Done."
MAX_TEAMS_CARD_TEXT_BYTES = 20 * 1024
TRUNCATION_MARKER = "\n\n[Response truncated to fit Microsoft Teams.]"


def bounded_text(text: str) -> str:
    """Cap `text` at the Teams card budget, cutting on a UTF-8 boundary.

    The truncation marker is part of the budget: a clipped string always
    fits, never silently drops the marker itself.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TEAMS_CARD_TEXT_BYTES:
        return text
    budget = MAX_TEAMS_CARD_TEXT_BYTES - len(TRUNCATION_MARKER.encode("utf-8"))
    clipped = encoded[:budget]
    while True:
        try:
            return clipped.decode("utf-8") + TRUNCATION_MARKER
        except UnicodeDecodeError:
            clipped = clipped[:-1]


def terminal_card(text: str) -> MessageActivityInput:
    """Build the single bounded card used for a terminal create or update."""
    text = bounded_text(text)
    card = AdaptiveCard(
        body=[TeamsTextBlock(text=text, wrap=True)],
        fallback_text=text,
    )
    return MessageActivityInput().add_card(card)


__all__ = [
    "FAILURE_MESSAGE",
    "INTERRUPTED_MESSAGE",
    "MAX_TEAMS_CARD_TEXT_BYTES",
    "NO_ANSWER_MESSAGE",
    "TRUNCATION_MARKER",
    "WORKING_MESSAGE",
    "bounded_text",
    "terminal_card",
]
