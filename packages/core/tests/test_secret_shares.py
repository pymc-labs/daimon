"""The share rules shared by /share-secret and `share_secret_with`."""

import re
import uuid
from datetime import UTC, datetime

from daimon.core import secret_shares as words


def test_a_label_is_one_tidy_line_of_at_most_100_characters() -> None:
    assert words.clean_label("  Prod   DB ") == "Prod DB"
    assert words.clean_label("a\nb") is None
    assert words.clean_label("   ") is None
    assert words.clean_label("x" * 101) is None


def test_a_value_fits_the_one_time_code_block() -> None:
    assert words.value_ok("s3cret")
    assert not words.value_ok("")
    assert not words.value_ok(" \n")
    assert not words.value_ok("a```b")
    assert not words.value_ok("x" * (words.MAX_VALUE + 1))


def test_a_label_cannot_mention_or_break_out_of_bold() -> None:
    text = words.card_text(
        sender="1", recipient="2", label="**x** <@3> @everyone", expires_at=datetime.now(UTC)
    )
    assert "<@3>" not in text and re.findall(r"<@(\d+)>", text) == ["2", "1"]
    assert "\\*\\*x\\*\\*" in text


def test_the_button_id_round_trips_the_share_id() -> None:
    share_id = str(uuid.uuid4())
    match = re.fullmatch(words.CUSTOM_ID_TEMPLATE, words.build_custom_id(share_id))
    assert match is not None and match["share_id"] == share_id
