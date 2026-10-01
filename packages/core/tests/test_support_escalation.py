"""Pure-logic tests for the support-escalation gate and custom_id grammar."""

from __future__ import annotations

import pytest
from daimon.core.message_feedback import CUSTOM_ID_PREFIX as FEEDBACK_PREFIX
from daimon.core.message_feedback import THUMBS_DOWN, THUMBS_UP, vote_for_reaction
from daimon.core.support_escalation import (
    CUSTOM_ID_PATTERN,
    CUSTOM_ID_PREFIX,
    ESCALATE,
    build_custom_id,
    has_credit,
    is_escalation_reaction,
    parse_custom_id,
    remaining_credits,
)


def test_remaining_credits_floors_at_zero_when_allowance_is_lowered() -> None:
    # Allowance is deployment config and can be cut below what someone already
    # spent. A negative would render as "-2 left" and invites a caller to
    # compare it against > 0 and disagree with has_credit.
    assert remaining_credits(allowance=1, used=3) == 0
    assert has_credit(allowance=1, used=3) is False


def test_has_credit_boundary() -> None:
    assert has_credit(allowance=3, used=2) is True
    assert has_credit(allowance=3, used=3) is False
    assert has_credit(allowance=0, used=0) is False


def test_escalate_emoji_is_not_a_vote_and_thumbs_are_not_escalations() -> None:
    # The two paths share the seeded reactions and nothing else. If either
    # classifier grew to match the other's emoji, an escalation would land in
    # message_feedback or a thumbs-down would spend a support credit.
    common = dict(emoji_is_custom=False, reactor_id=2, bot_user_id=1, guild_id=99)
    assert vote_for_reaction(emoji_name=ESCALATE, **common) is None
    assert is_escalation_reaction(emoji_name=THUMBS_UP, **common) is False
    assert is_escalation_reaction(emoji_name=THUMBS_DOWN, **common) is False
    assert is_escalation_reaction(emoji_name=ESCALATE, **common) is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"guild_id": None},  # a DM reaction resolves no tenant
        {"reactor_id": 1},  # the bot's own seeded reaction
        {"emoji_is_custom": True},  # a custom emoji that happens to be named alike
    ],
)
def test_escalation_gates(kwargs: dict[str, object]) -> None:
    base = dict(
        emoji_name=ESCALATE,
        emoji_is_custom=False,
        reactor_id=2,
        bot_user_id=1,
        guild_id=99,
    )
    assert is_escalation_reaction(**{**base, **kwargs}) is False  # type: ignore[arg-type]


def test_custom_id_roundtrip() -> None:
    cid = build_custom_id(guild_id="1", channel_id="22", message_id="333")
    assert parse_custom_id(cid) == ("1", "22", "333")


def test_custom_id_prefix_is_disjoint_from_the_feedback_button() -> None:
    # discord.py fullmatches an incoming custom_id against EVERY registered
    # dynamic-item template with no early break, so two overlapping prefixes
    # would fire both handlers on one click.
    assert not CUSTOM_ID_PREFIX.startswith(FEEDBACK_PREFIX)
    assert not FEEDBACK_PREFIX.startswith(CUSTOM_ID_PREFIX)
    assert CUSTOM_ID_PATTERN.fullmatch("mfb:not-ours") is None


def test_parse_rejects_malformed() -> None:
    assert parse_custom_id("sup:1:2") is None
    assert parse_custom_id("sup:abc:2:3") is None
    assert parse_custom_id("") is None


def test_escalation_is_disabled_when_no_channel_is_configured() -> None:
    """An escalate path that reaches nobody is worse than none at all.

    The person believes they have asked for help. Failing closed here is why
    both the reaction gate and the submit path check the channel, rather than
    recording requests into a table nobody watches.
    """
    from daimon.core.config import SupportSettings

    assert SupportSettings().escalation_channel_id is None, (
        "escalation must be OFF by default -- a deployment that never "
        "configures a channel must not offer the affordance"
    )
    assert SupportSettings().credits_per_user == 3


def test_support_credits_are_not_the_billing_ledger() -> None:
    """Support credits are a COUNT; signup_credit is USD in tenant_ledger.

    Sharing one ledger would let a support request consume the tenant's
    ability to run turns, and would hand a paid-up tenant unlimited support.
    Asserted rather than only documented so a later 'tidy-up' that merges them
    fails here.
    """
    from daimon.core.config import BillingSettings, SupportSettings

    assert isinstance(SupportSettings().credits_per_user, int)
    assert not isinstance(BillingSettings().signup_credit, int)
