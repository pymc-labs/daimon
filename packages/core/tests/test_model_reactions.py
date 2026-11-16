"""The emoji that name a turn's model and effort."""

from __future__ import annotations

import pytest
from daimon.core.model_reactions import model_marks


@pytest.mark.parametrize(
    ("model_id", "effort", "expected"),
    [
        ("claude-haiku-5-5", "low", ("🍃", "🌑")),
        ("claude-sonnet-5-5", "medium", ("🪶", "🌘")),
        ("claude-opus-4-8", "xhigh", ("🎼", "🌖")),
        ("claude-fable-5-2", "max", ("🦊", "🌕")),
        ("claude-sonnet-5-5", None, ("🪶",)),
        ("gpt-6", "high", ("🌓",)),
        (None, None, ()),
    ],
)
def test_marks_name_the_model_and_the_effort(
    model_id: str | None, effort: str | None, expected: tuple[str, ...]
) -> None:
    """A model family and an effort level each have one emoji; unknown ones have none."""
    assert model_marks(model_id, effort) == expected, f"marks for {model_id} at {effort}"
