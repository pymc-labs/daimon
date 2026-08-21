"""Pure vote-decision logic and the feedback-button `custom_id` grammar.

This module lives in `daimon.core`, not the Discord adapter, for the same
reason `daimon.core.credential_requests` does: the encode/decode contract for
a persistent-button `custom_id` belongs one level above the adapter that
renders the widget, so any future caller (a store, a reaction listener, a
button handler) can share the same grammar without importing an adapter
package.

`CUSTOM_ID_PATTERN` MUST fullmatch every minted custom_id, and its prefix
must stay disjoint from every other registered dynamic-item template's
prefix. discord.py's `dispatch_dynamic_items` calls `pattern.fullmatch` on an
incoming custom_id against EVERY registered template, with no early break --
two overlapping templates would both fullmatch a single click and both
handlers would fire. `CUSTOM_ID_PREFIX` here (`mfb:`) is therefore
deliberately disjoint from the credential-request button's `ztc:` prefix.

The custom_id carries the feedback row's own primary key (a UUID) rather
than the Discord message id, because the button is delivered into a direct
message, where no guild id is available to reconstruct the tenant-scoped
`(tenant_id, message_id, platform_user_id)` uniqueness key the vote is
actually keyed on -- the row id is the only handle that is self-sufficient
there. It is a random UUID, so it is not enumerable, and the write path that
consumes it additionally gates on the clicking user's own platform id.

Capture differs per platform. Discord seeds thumbs reactions and listens for
them; a reaction event carries no interaction handle, so Discord bridges a
down-vote into private feedback via a button delivered in a direct message
(the `custom_id` grammar above). Slack deliberately does NOT use reactions:
observing them would need the `reactions:read` bot scope and the DM bridge
would need `im:write`, and adding either scope forces every installed
workspace through a re-authorization flow. Slack instead renders vote
buttons on the final answer message itself
(`daimon.adapters.slack.feedback`) -- a button click is a block_actions
payload whose `trigger_id` opens the feedback modal directly, so neither
scope is needed and `vote_for_reaction`/`is_bot_authored` below have no
Slack callers. `tests/parity/test_message_feedback_discord_only.py` is the
executable record of that split -- it fails if either scope quietly appears
or the Slack button surface disappears.
"""

from __future__ import annotations

import re
from typing import Final, Literal

Vote = Literal["up", "down"]

THUMBS_UP: Final[str] = "\N{THUMBS UP SIGN}"
THUMBS_DOWN: Final[str] = "\N{THUMBS DOWN SIGN}"

CUSTOM_ID_PREFIX: Final[str] = "mfb:"

CUSTOM_ID_TEMPLATE: Final[str] = (
    r"mfb:(?P<feedback_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
CUSTOM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(CUSTOM_ID_TEMPLATE)


def build_custom_id(feedback_id: str) -> str:
    """Return the wire `custom_id` for a feedback row's primary key."""
    return f"{CUSTOM_ID_PREFIX}{feedback_id}"


def vote_for_reaction(
    *,
    emoji_name: str | None,
    emoji_is_custom: bool,
    reactor_id: int,
    bot_user_id: int,
    guild_id: int | None,
) -> Vote | None:
    """Classify a raw reaction-add gateway payload as a vote, or not.

    Returns `None` (not a vote) when any of the following hold: the reaction
    has no resolvable guild (a DM reaction resolves no tenant, so it is
    ignored entirely, not attributed to any install); the reactor is the bot
    itself (the bot's own seeded reactions must never count as votes); the
    emoji is a custom (non-Unicode) emoji; or the emoji is not exactly
    `THUMBS_UP` or `THUMBS_DOWN`. Skin-tone-modified thumbs emoji are
    deliberately NOT matched -- they are distinct codepoint sequences from
    the bare emoji this module seeds, and matching them would require
    enumerating every Fitzpatrick modifier combination for no behavioral
    benefit.

    Deliberately does not take `message_author_id`: whether the reacted-to
    message was bot-authored is a separate question, answered by
    `is_bot_authored`, because that field is not reliably present on every
    gateway payload that carries a reaction.
    """
    if guild_id is None:
        return None
    if reactor_id == bot_user_id:
        return None
    if emoji_is_custom:
        return None
    if emoji_name == THUMBS_UP:
        return "up"
    if emoji_name == THUMBS_DOWN:
        return "down"
    return None


def is_bot_authored(*, message_author_id: int | None, bot_user_id: int) -> bool | None:
    """Decide whether the reacted-to message was authored by the bot.

    Tri-state: returns `True`/`False` when `message_author_id` is known, and
    `None` when it is not -- meaning "undecidable from the gateway payload,
    the caller must fetch the message". The `None` case exists because the
    gateway field that carries a reaction event's message author is
    undocumented by Discord and may be absent; this function only owns the
    decision shape, the shell owns the REST fallback that resolves the `None`
    case.
    """
    if message_author_id is None:
        return None
    return message_author_id == bot_user_id


def should_trigger_feedback_dm(*, previous_vote: Vote | None, new_vote: Vote) -> bool:
    """Decide whether a new vote should open the private feedback path.

    A flip from `"up"` to `"down"` counts as a new down-vote and does
    trigger the private path; a repeat `"down"` (the vote is unchanged) does
    not.
    """
    return new_vote == "down" and previous_vote != "down"
