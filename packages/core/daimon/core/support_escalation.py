"""Pure logic for human-support escalation: the credit gate and the button grammar.

Lives in `daimon.core` for the same reason `message_feedback` does -- the
`custom_id` grammar belongs one level above the adapter that renders the
widget.

**Credits here are a COUNT of human interactions, not money.** They are
deliberately unrelated to `BillingSettings.signup_credit`, which is USD seeded
into `tenant_ledger` and gates turns through `is_over_cap`. The two must never
share a ledger: metering support against the billing balance would let a
support request consume the tenant's ability to run turns, and a paid-up
tenant would silently gain unlimited support. Different unit, different table,
different exhaustion behaviour.

There is no counter column. `support_escalations` rows ARE the ledger, and
`remaining` is the allowance minus the row count for that (tenant, user). A
counter would be a second source of truth that can drift from the rows it
claims to summarise, and the decrement would need to be transactionally
married to the insert anyway -- which counting already is, for free.

`ESCALATE` is a third seeded reaction alongside the two vote emoji. It is NOT
a vote: `vote_for_reaction` deliberately does not match it, so an escalation
never lands in `message_feedback`, and this module's classifier does not match
the thumbs. The two paths share the seeding call and nothing else.

The `sup:` prefix is disjoint from `mfb:` (message feedback) and `ztc:`
(credential requests), which is load-bearing: discord.py's
`dispatch_dynamic_items` fullmatches an incoming custom_id against EVERY
registered template with no early break, so two overlapping prefixes would
both fire.
"""

from __future__ import annotations

import re
from typing import Final

ESCALATE: Final[str] = "\N{HAPPY PERSON RAISING ONE HAND}"

CUSTOM_ID_PREFIX: Final[str] = "sup:"

CUSTOM_ID_TEMPLATE: Final[str] = (
    r"sup:(?P<guild_id>[0-9]{1,20}):(?P<channel_id>[0-9]{1,20})"
    r":(?P<message_id>[0-9]{1,20})"
)
CUSTOM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(CUSTOM_ID_TEMPLATE)


def build_custom_id(*, guild_id: str, channel_id: str, message_id: str) -> str:
    """Return the wire `custom_id` for the escalate button on one message.

    Carries ids rather than a row id because NO ROW EXISTS YET -- the row is
    written when the modal is submitted, not when the reaction lands.
    Reacting must not spend a credit; only sending the note does.

    `guild_id` is carried because the button is delivered into a direct
    message, where `interaction.guild_id` is None and the tenant would
    otherwise be underivable. It is not a trust boundary: the escalation is
    written against the CLICKING user's id and counted against their own
    allowance, so a forwarded custom_id spends the forwarder's credit in the
    named tenant rather than impersonating anyone.
    """
    return f"{CUSTOM_ID_PREFIX}{guild_id}:{channel_id}:{message_id}"


def parse_custom_id(custom_id: str) -> tuple[str, str, str] | None:
    """Return `(guild_id, channel_id, message_id)`, or `None` if it does not match."""
    match = CUSTOM_ID_PATTERN.fullmatch(custom_id)
    if match is None:
        return None
    return match.group("guild_id"), match.group("channel_id"), match.group("message_id")


def is_escalation_reaction(
    *,
    emoji_name: str | None,
    emoji_is_custom: bool,
    reactor_id: int,
    bot_user_id: int,
    guild_id: int | None,
) -> bool:
    """Classify a raw reaction-add payload as an escalation request, or not.

    Same gates, and the same reasons, as `message_feedback.vote_for_reaction`:
    a reaction with no guild resolves no tenant, the bot's own seeded reaction
    is not a request, and a custom emoji is not the bare codepoint this module
    seeds. Skin-tone-modified variants are deliberately not matched -- they are
    distinct codepoint sequences from what `seed_feedback_reactions` adds.
    """
    if guild_id is None:
        return False
    if reactor_id == bot_user_id:
        return False
    if emoji_is_custom:
        return False
    return emoji_name == ESCALATE


def remaining_credits(*, allowance: int, used: int) -> int:
    """Credits left for one person, floored at zero.

    Floors rather than returning a negative because `allowance` is
    deployment config and can be lowered below what somebody has already
    spent. A negative remaining would render as "-2 credits left" and, worse,
    invites a caller to compare it with `> 0` somewhere and get a different
    answer than `has_credit` gives.
    """
    return max(allowance - used, 0)


def has_credit(*, allowance: int, used: int) -> bool:
    """Whether one more escalation is permitted."""
    return remaining_credits(allowance=allowance, used=used) > 0
