"""`daimon.core.cursor`: the opaque page marker a list hands back."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.cursor import decode_cursor, encode_cursor


def test_a_cursor_round_trips() -> None:
    """What a list encodes comes back exactly, microseconds and zone included."""
    cursor = (datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC), uuid.uuid4())

    text = encode_cursor(cursor)

    assert decode_cursor(text) == cursor, "the decoded cursor equals the encoded one"
    assert "=" not in text and "|" not in text, "the text is safe in a URL"


@pytest.mark.parametrize("text", ["", "nope", "bm9wZQ", "YXxi"])
def test_a_made_up_cursor_is_refused(text: str) -> None:
    """Anything a list did not hand out is a ValueError, never a crash further in."""
    with pytest.raises(ValueError, match="not a cursor"):
        decode_cursor(text)
