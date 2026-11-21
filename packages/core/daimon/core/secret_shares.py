"""One secret shared with one person: its limits, the card's words and the reveal refusals.

Discord only: the card and the reveal button live in a DM (see
`tests/parity/test_secret_sharing_discord_only.py`). The value is encrypted at rest, shown
once to the recipient, and cleared on reveal or expiry; the row stays as the audit record.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Final, Literal

SHARE_LIFETIME: Final = timedelta(hours=24)
MAX_LABEL: Final = 100
MAX_VALUE: Final = 1700
MAX_LIVE_PER_SENDER: Final = 10

CARD_COLOR: Final = 0x5865F2  # the neutral notice colour
CUSTOM_ID_PREFIX: Final = "sss:"
CUSTOM_ID_TEMPLATE: Final[str] = (
    r"sss:(?P<share_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

RevealRefusal = Literal["not_recipient", "revealed", "expired", "gone"]
REVEAL_REFUSALS: Final[dict[RevealRefusal, str]] = {
    "not_recipient": "Only the person this was shared with can reveal it.",
    "revealed": "This has already been revealed, and the secret is deleted.",
    "expired": "This secret expired before it was revealed and has been deleted.",
    "gone": "This is no longer available.",
}

OFF: Final = "Sharing secrets is off here. Ask an admin to turn on Secret sharing."
BAD_LABEL: Final = f"Give the secret a name of 1 to {MAX_LABEL} characters on one line."
BAD_VALUE: Final = f"The secret must be 1 to {MAX_VALUE:,} characters, with no ``` in it."
TOO_MANY: Final = (
    f"You have {MAX_LIVE_PER_SENDER} secrets waiting to be revealed. "
    "Wait for one to be revealed or to expire."
)
NOT_A_PERSON: Final = "Share it with a person in this server."
NO_KEY: Final = (
    "Nothing was shared: this Daimon install has no encryption key. Ask whoever runs it to add one."
)


def build_custom_id(share_id: str) -> str:
    return f"{CUSTOM_ID_PREFIX}{share_id}"


def clean_label(label: str) -> str | None:
    """The label with its spaces tidied, or None when it is empty, too long or multi-line."""
    if "\n" in label or "\r" in label:
        return None
    cleaned = " ".join(label.split())
    return cleaned if 0 < len(cleaned) <= MAX_LABEL else None


def value_ok(value: str) -> bool:
    """Whether a value fits the one-time reply's code block."""
    return 0 < len(value) <= MAX_VALUE and value.strip() != "" and "```" not in value


def _bold(label: str) -> str:
    return "**" + re.sub(r"([\\*_~|`>#<@\[\]()-])", r"\\\1", label) + "**"


def card_text(*, sender: str, recipient: str, label: str, expires_at: datetime) -> str:
    return (
        f"<@{recipient}>: <@{sender}> shared {_bold(label)} with you. Only you can reveal it, "
        f"and only once. It expires <t:{int(expires_at.timestamp())}:R>."
    )


def revealed_text(*, sender: str, recipient: str, label: str, at: datetime) -> str:
    return (
        f"{_bold(label)} from <@{sender}> was revealed by <@{recipient}> "
        f"at <t:{int(at.timestamp())}:F>."
    )


def expired_text(*, sender: str, label: str) -> str:
    return f"{_bold(label)} from <@{sender}> expired before it was revealed."


def reveal_reply(*, sender: str, label: str, value: str) -> str:
    return f"{_bold(label)} from <@{sender}>:\n```\n{value}\n```\nShown once; it is deleted now."


def shared_text(*, recipient: str, label: str) -> str:
    return f"Sent {_bold(label)} to <@{recipient}>'s DMs. Only they can reveal it, once."


def dm_closed_text(recipient: str) -> str:
    return f"<@{recipient}> doesn't take DMs from Daimon, so nothing was shared."
