"""Slack's modal limits as code, and the one builder every panel view ends in.

Slack rejects an over-limit view outright: the API call fails and the person
sees nothing. A renderer that trusts its own arithmetic therefore fails in
production, not in a test — so every limit the panel can run into is a named
constant here, and `finish_modal` checks each one before a view leaves the
process.

The numbers are Slack's, from the modal-view reference
(https://docs.slack.dev/reference/views/modal-views) and the modals guide
(https://docs.slack.dev/surfaces/modals): 100 blocks per view, 24 characters
for each of `title` / `close` / `submit`, 3,000 for `private_metadata`, 3,000
for a section's text, and at most 3 views in one modal's stack.

Pure — no I/O, no slack_sdk import.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

__all__ = [
    "MAX_BLOCKS_PER_VIEW",
    "MAX_PRIVATE_METADATA_CHARS",
    "MAX_SECTION_TEXT_CHARS",
    "MAX_TITLE_CHARS",
    "MAX_VIEW_STACK",
    "finish_modal",
    "fit_title",
]

#: Blocks in one view's `blocks` array.
MAX_BLOCKS_PER_VIEW: Final = 100

#: Characters in `title`, `close` and `submit` — the same cap for all three.
MAX_TITLE_CHARS: Final = 24

#: Characters in `private_metadata`.
MAX_PRIVATE_METADATA_CHARS: Final = 3000

#: Views one modal's stack holds. Recorded for callers that push; the panel
#: itself uses two (a root plus one pushed view).
MAX_VIEW_STACK: Final = 3

#: Characters in a section block's `text`. Renderers clip to this themselves —
#: a section that grows with an agent's key list is content, not a bug.
MAX_SECTION_TEXT_CHARS: Final = 3000

_ELLIPSIS: Final = "…"


def fit_title(text: str) -> str:
    """Return `text` cut to Slack's title cap, marked when it was cut.

    A title over the cap is rejected by Slack rather than trimmed, so the
    trimming happens here. The ellipsis costs one of the 24 characters, which
    is the point: the reader can tell the name is incomplete and look for the
    full one in the body.
    """
    if len(text) <= MAX_TITLE_CHARS:
        return text
    return f"{text[: MAX_TITLE_CHARS - 1]}{_ELLIPSIS}"


def finish_modal(
    *,
    title: str,
    blocks: Sequence[dict[str, Any]],
    private_metadata: str,
    callback_id: str,
    close: str = "Done",
    submit: str | None = None,
) -> dict[str, Any]:
    """Wrap `blocks` in a modal view, refusing to build an over-limit one.

    Raises `ValueError` naming the limit that was broken rather than handing
    Slack a payload it will reject: a caller that miscounted gets a stack
    trace pointing at its own view, not an opaque `invalid_blocks` from the
    API. `notify_on_close` is False because closing the panel is not an event
    the panel needs to hear about.
    """
    if not title:
        raise ValueError("a modal title must not be empty")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"modal title is {len(title)} characters; Slack allows {MAX_TITLE_CHARS}")
    if not close or len(close) > MAX_TITLE_CHARS:
        raise ValueError(
            f"modal close text is {len(close)} characters; Slack allows 1 to {MAX_TITLE_CHARS}"
        )
    if submit is not None and (not submit or len(submit) > MAX_TITLE_CHARS):
        raise ValueError(
            f"modal submit text is {len(submit)} characters; Slack allows 1 to {MAX_TITLE_CHARS}"
        )
    if len(blocks) > MAX_BLOCKS_PER_VIEW:
        raise ValueError(f"modal carries {len(blocks)} blocks; Slack allows {MAX_BLOCKS_PER_VIEW}")
    if len(private_metadata) > MAX_PRIVATE_METADATA_CHARS:
        raise ValueError(
            f"private_metadata is {len(private_metadata)} characters; "
            f"Slack allows {MAX_PRIVATE_METADATA_CHARS}"
        )
    view: dict[str, Any] = {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": close},
        "notify_on_close": False,
        "blocks": list(blocks),
    }
    if submit is not None:
        view["submit"] = {"type": "plain_text", "text": submit}
    return view
