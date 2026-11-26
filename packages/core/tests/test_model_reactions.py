"""The emoji that name a conversation's model and effort."""

from __future__ import annotations

from daimon.core.model_reactions import swapped_marks


def test_swapped_marks_swaps_what_was_picked_and_keeps_the_rest() -> None:
    shown = ("🎼", "🌓")
    assert swapped_marks(shown, "claude-sonnet-5-5", None) == ("🪶", "🌓"), "the effort stays"
    assert swapped_marks(shown, None, "max") == ("🎼", "🌕"), "the model stays"
    assert swapped_marks(shown, "claude-haiku-5-5", "low") == ("🍃", "🌑")
