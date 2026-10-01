"""Slack's modal limits, checked at the boundary that enforces them.

`finish_modal` exists so an over-limit view fails here rather than at the
Slack API, where the person sees an unexplained nothing. These tests pin both
sides of every limit: exactly at the cap builds, one past it raises.
"""

from __future__ import annotations

from typing import Any

import pytest
from daimon.adapters.slack.modal_limits import (
    MAX_BLOCKS_PER_VIEW,
    MAX_PRIVATE_METADATA_CHARS,
    MAX_TITLE_CHARS,
    MAX_VIEW_STACK,
    finish_modal,
    fit_title,
)


def _blocks(count: int) -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"row {index}"}}
        for index in range(count)
    ]


def _modal(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "title": "Agents",
        "blocks": _blocks(1),
        "private_metadata": "{}",
        "callback_id": "agent_setup",
    }
    kwargs.update(overrides)
    return finish_modal(**kwargs)


# ---------------------------------------------------------------------------
# fit_title
# ---------------------------------------------------------------------------


def test_fit_title_when_at_the_cap_returns_the_text_unchanged() -> None:
    text = "x" * MAX_TITLE_CHARS
    assert fit_title(text) == text, "a title that already fits must not be altered"


def test_fit_title_when_one_over_the_cap_marks_the_cut() -> None:
    text = "x" * (MAX_TITLE_CHARS + 1)
    fitted = fit_title(text)
    assert len(fitted) == MAX_TITLE_CHARS, "a fitted title must land exactly on Slack's cap"
    assert fitted.endswith("…"), "a cut title must say it was cut"
    assert fitted[:-1] == "x" * (MAX_TITLE_CHARS - 1), (
        "the kept prefix must be the start of the original name"
    )


def test_fit_title_when_short_returns_the_text_unchanged() -> None:
    assert fit_title("Agents") == "Agents", "a short title must pass through untouched"


# ---------------------------------------------------------------------------
# finish_modal — shape
# ---------------------------------------------------------------------------


def test_finish_modal_sets_the_modal_shape_slack_expects() -> None:
    view = _modal(private_metadata='{"t":"T1"}', callback_id="agent_setup__details_view")
    assert view["type"] == "modal", "every panel view is a modal"
    assert view["callback_id"] == "agent_setup__details_view", "the callback id is passed through"
    assert view["private_metadata"] == '{"t":"T1"}', "private_metadata is passed through verbatim"
    assert view["title"] == {"type": "plain_text", "text": "Agents"}, (
        "the title must be a plain_text object"
    )
    assert view["close"] == {"type": "plain_text", "text": "Done"}, (
        "close defaults to Done as a plain_text object"
    )
    assert view["notify_on_close"] is False, "the panel does not listen for its own close"
    assert "submit" not in view, "a view with no submit text must not carry a submit button"


def test_finish_modal_when_submit_given_renders_it_as_plain_text() -> None:
    view = _modal(submit="Create", close="Cancel")
    assert view["submit"] == {"type": "plain_text", "text": "Create"}, (
        "submit must be a plain_text object"
    )
    assert view["close"] == {"type": "plain_text", "text": "Cancel"}, (
        "an explicit close must override the default"
    )


def test_finish_modal_copies_the_block_sequence_it_was_given() -> None:
    blocks = _blocks(2)
    view = _modal(blocks=blocks)
    blocks.append({"type": "divider"})
    assert len(view["blocks"]) == 2, "the built view must not alias the caller's list"


# ---------------------------------------------------------------------------
# finish_modal — limits
# ---------------------------------------------------------------------------


def test_finish_modal_at_every_limit_builds_a_view() -> None:
    view = _modal(
        title="x" * MAX_TITLE_CHARS,
        close="y" * MAX_TITLE_CHARS,
        submit="z" * MAX_TITLE_CHARS,
        blocks=_blocks(MAX_BLOCKS_PER_VIEW),
        private_metadata="m" * MAX_PRIVATE_METADATA_CHARS,
    )
    assert len(view["blocks"]) == MAX_BLOCKS_PER_VIEW, (
        "a view sitting exactly on every limit must still build"
    )


def test_finish_modal_when_over_the_block_cap_raises() -> None:
    with pytest.raises(ValueError, match="blocks"):
        _modal(blocks=_blocks(MAX_BLOCKS_PER_VIEW + 1))


def test_finish_modal_when_title_over_the_cap_raises() -> None:
    with pytest.raises(ValueError, match="title"):
        _modal(title="x" * (MAX_TITLE_CHARS + 1))


def test_finish_modal_when_close_over_the_cap_raises() -> None:
    with pytest.raises(ValueError, match="close"):
        _modal(close="x" * (MAX_TITLE_CHARS + 1))


def test_finish_modal_when_submit_over_the_cap_raises() -> None:
    with pytest.raises(ValueError, match="submit"):
        _modal(submit="x" * (MAX_TITLE_CHARS + 1))


def test_finish_modal_when_private_metadata_over_the_cap_raises() -> None:
    with pytest.raises(ValueError, match="private_metadata"):
        _modal(private_metadata="m" * (MAX_PRIVATE_METADATA_CHARS + 1))


def test_finish_modal_when_title_empty_raises() -> None:
    with pytest.raises(ValueError, match="title"):
        _modal(title="")


def test_view_stack_limit_matches_slacks_documented_depth() -> None:
    assert MAX_VIEW_STACK == 3, "Slack holds at most three views in one modal's stack"
