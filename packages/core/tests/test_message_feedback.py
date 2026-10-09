"""Unit tests for the message-feedback vote logic and custom_id grammar (no I/O, no DB)."""

from __future__ import annotations

import uuid

from daimon.core.credential_requests import CUSTOM_ID_PATTERN as CREDENTIAL_CUSTOM_ID_PATTERN
from daimon.core.credential_requests import build_custom_id as build_credential_custom_id
from daimon.core.credential_requests import mint_request_token
from daimon.core.message_feedback import (
    CUSTOM_ID_PATTERN,
    FEEDBACK_REASONS,
    THUMBS_DOWN,
    THUMBS_UP,
    build_custom_id,
    is_bot_authored,
    known_feedback_reasons,
    should_trigger_feedback_dm,
    vote_for_reaction,
)

BOT_USER_ID = 1
REACTOR_ID = 2
GUILD_ID = 9


# --- vote_for_reaction ---


def test_vote_for_reaction_thumbs_up_by_non_bot_user_in_guild_returns_up() -> None:
    assert (
        vote_for_reaction(
            emoji_name=THUMBS_UP,
            emoji_is_custom=False,
            reactor_id=REACTOR_ID,
            bot_user_id=BOT_USER_ID,
            guild_id=GUILD_ID,
        )
        == "up"
    ), "a plain thumbs-up by a non-bot user in a guild must count as an up-vote"


def test_vote_for_reaction_thumbs_down_by_non_bot_user_in_guild_returns_down() -> None:
    assert (
        vote_for_reaction(
            emoji_name=THUMBS_DOWN,
            emoji_is_custom=False,
            reactor_id=REACTOR_ID,
            bot_user_id=BOT_USER_ID,
            guild_id=GUILD_ID,
        )
        == "down"
    ), "a plain thumbs-down by a non-bot user in a guild must count as a down-vote"


def test_vote_for_reaction_reactor_is_bot_returns_none() -> None:
    assert (
        vote_for_reaction(
            emoji_name=THUMBS_DOWN,
            emoji_is_custom=False,
            reactor_id=BOT_USER_ID,
            bot_user_id=BOT_USER_ID,
            guild_id=GUILD_ID,
        )
        is None
    ), "the bot's own seeded reactions must never count as votes for themselves"


def test_vote_for_reaction_no_guild_returns_none_even_for_valid_thumbs_emoji() -> None:
    assert (
        vote_for_reaction(
            emoji_name=THUMBS_UP,
            emoji_is_custom=False,
            reactor_id=REACTOR_ID,
            bot_user_id=BOT_USER_ID,
            guild_id=None,
        )
        is None
    ), "a DM reaction resolves no tenant and must be ignored entirely"


def test_vote_for_reaction_custom_emoji_returns_none() -> None:
    assert (
        vote_for_reaction(
            emoji_name=THUMBS_UP,
            emoji_is_custom=True,
            reactor_id=REACTOR_ID,
            bot_user_id=BOT_USER_ID,
            guild_id=GUILD_ID,
        )
        is None
    ), "a custom (non-Unicode) emoji must never count as a vote"


def test_vote_for_reaction_emoji_name_none_returns_none() -> None:
    assert (
        vote_for_reaction(
            emoji_name=None,
            emoji_is_custom=False,
            reactor_id=REACTOR_ID,
            bot_user_id=BOT_USER_ID,
            guild_id=GUILD_ID,
        )
        is None
    ), "a missing emoji name must never count as a vote"


def test_vote_for_reaction_unrelated_emoji_returns_none() -> None:
    assert (
        vote_for_reaction(
            emoji_name="\N{PARTY POPPER}",
            emoji_is_custom=False,
            reactor_id=REACTOR_ID,
            bot_user_id=BOT_USER_ID,
            guild_id=GUILD_ID,
        )
        is None
    ), "an emoji that is neither thumbs-up nor thumbs-down must not count as a vote"


def test_vote_for_reaction_skin_tone_thumbs_up_returns_none() -> None:
    skin_toned = THUMBS_UP + "\N{EMOJI MODIFIER FITZPATRICK TYPE-3}"
    assert (
        vote_for_reaction(
            emoji_name=skin_toned,
            emoji_is_custom=False,
            reactor_id=REACTOR_ID,
            bot_user_id=BOT_USER_ID,
            guild_id=GUILD_ID,
        )
        is None
    ), "skin-tone variants are distinct codepoint sequences and are deliberately not matched"


# --- is_bot_authored ---


def test_is_bot_authored_true_when_author_matches_bot() -> None:
    assert is_bot_authored(message_author_id=BOT_USER_ID, bot_user_id=BOT_USER_ID) is True, (
        "a message authored by the bot's own id must be classified as bot-authored"
    )


def test_is_bot_authored_false_when_author_differs() -> None:
    assert is_bot_authored(message_author_id=REACTOR_ID, bot_user_id=BOT_USER_ID) is False, (
        "a message authored by a different id must be classified as not bot-authored"
    )


def test_is_bot_authored_none_when_author_id_is_none() -> None:
    result = is_bot_authored(message_author_id=None, bot_user_id=BOT_USER_ID)
    assert result is None, (
        "an absent author id must return None (undecidable), not False -- False means "
        "'not ours' while None means 'must fetch to find out', and the two are not the same"
    )


# --- should_trigger_feedback_dm ---


def test_should_trigger_feedback_dm_first_downvote_returns_true() -> None:
    assert should_trigger_feedback_dm(previous_vote=None, new_vote="down") is True, (
        "a first-ever down-vote must trigger the private feedback path"
    )


def test_should_trigger_feedback_dm_flip_from_up_to_down_returns_true() -> None:
    assert should_trigger_feedback_dm(previous_vote="up", new_vote="down") is True, (
        "a flip from up to down must count as a new down-vote and trigger the private path"
    )


def test_should_trigger_feedback_dm_repeat_downvote_returns_false() -> None:
    assert should_trigger_feedback_dm(previous_vote="down", new_vote="down") is False, (
        "a repeat down-vote (no state change) must not re-trigger the private path"
    )


def test_should_trigger_feedback_dm_upvote_never_triggers() -> None:
    for previous_vote in (None, "up", "down"):
        assert should_trigger_feedback_dm(previous_vote=previous_vote, new_vote="up") is False, (
            f"an up-vote must never trigger the private path regardless of previous_vote="
            f"{previous_vote!r}"
        )


# --- custom_id grammar ---


def test_custom_id_pattern_fullmatches_and_round_trips_feedback_id() -> None:
    feedback_id = str(uuid.uuid4())
    custom_id = build_custom_id(feedback_id)

    match = CUSTOM_ID_PATTERN.fullmatch(custom_id)

    assert match is not None, "the template must fullmatch a real minted feedback custom_id"
    assert match["feedback_id"] == feedback_id, (
        "the feedback_id group must round-trip the input uuid string"
    )


def test_custom_id_pattern_matches_lowercase_and_uppercase_hex_uuid() -> None:
    lowercase = str(uuid.uuid4())
    uppercase = lowercase.upper()

    assert CUSTOM_ID_PATTERN.fullmatch(build_custom_id(lowercase)) is not None, (
        "a lowercase-hex uuid must fullmatch"
    )
    assert CUSTOM_ID_PATTERN.fullmatch(build_custom_id(uppercase)) is not None, (
        "an uppercase-hex uuid must fullmatch"
    )


def test_custom_id_pattern_rejects_bare_uuid_with_no_prefix() -> None:
    bare = str(uuid.uuid4())
    assert CUSTOM_ID_PATTERN.fullmatch(bare) is None, (
        "a custom_id missing the mfb: prefix must not match"
    )


def test_feedback_custom_id_does_not_fullmatch_credential_template() -> None:
    feedback_custom_id = build_custom_id(str(uuid.uuid4()))
    assert CREDENTIAL_CUSTOM_ID_PATTERN.fullmatch(feedback_custom_id) is None, (
        "discord.py fires every registered dynamic-item template whose pattern fullmatches "
        "an incoming custom_id, with no early break, so a feedback custom_id must never also "
        "fullmatch the credential-request template -- overlap would double-dispatch one click"
    )


def test_credential_custom_id_does_not_fullmatch_feedback_template() -> None:
    credential_custom_id = build_credential_custom_id(mint_request_token())
    assert CUSTOM_ID_PATTERN.fullmatch(credential_custom_id) is None, (
        "discord.py fires every registered dynamic-item template whose pattern fullmatches "
        "an incoming custom_id, with no early break, so a credential-request custom_id must "
        "never also fullmatch the feedback template -- overlap would double-dispatch one click"
    )


def test_known_feedback_reasons_keeps_vocabulary_order_once_each() -> None:
    assert known_feedback_reasons(["other", "inaccurate", "other"]) == ["inaccurate", "other"]


def test_known_feedback_reasons_drops_codes_outside_the_vocabulary() -> None:
    assert known_feedback_reasons(["", "Inaccurate", "drop table"]) == []


def test_feedback_reason_codes_are_the_stored_contract() -> None:
    """Renaming a code orphans every row that stored it; change the label instead."""
    assert list(FEEDBACK_REASONS) == [
        "inaccurate",
        "ignored_request",
        "incomplete",
        "too_slow",
        "other",
    ]
