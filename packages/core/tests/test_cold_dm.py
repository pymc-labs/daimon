"""The reply to a private message that has no conversation to continue."""

from __future__ import annotations

from daimon.core.direct_messages import ColdDmNotices, cold_dm_text


def test_private_chats_off_names_the_platforms_own_admin_and_command() -> None:
    assert cold_dm_text("discord", dms_on=False) == (
        "Private chats with Daimon are off here.\n"
        "Mention me in a channel, or ask a server admin to run `/dm-me enable`."
    )
    assert cold_dm_text("slack", dms_on=False) == (
        "Private chats with Daimon are off here.\n"
        "Mention me in a channel, or ask a workspace admin to run `/dm-me enable`."
    )


def test_private_chats_on_points_to_dm_me_in_a_channel() -> None:
    assert cold_dm_text("slack", dms_on=True) == (
        "To talk here, run `/dm-me` in a channel first and I'll continue with you here.\n"
        "Or mention me in any channel."
    )


def test_unknown_space_names_no_server_and_does_not_claim_chats_are_off() -> None:
    text = cold_dm_text("discord", dms_on=None)
    assert text.startswith("To talk here, run `/dm-me` in a server channel first")
    assert "are off here" not in text
    assert "ask a server admin to run `/dm-me enable`" in text


def test_each_person_is_told_once() -> None:
    notices = ColdDmNotices()
    assert notices.first_time("a") is True
    assert notices.first_time("a") is False
    assert notices.first_time("b") is True


def test_the_set_is_bounded_and_forgets_the_oldest_first() -> None:
    notices = ColdDmNotices(limit=2)
    for person in ("a", "b", "c"):
        notices.first_time(person)
    assert notices.first_time("c") is False, "recent people stay remembered"
    assert notices.first_time("a") is True, "the oldest was dropped to stay within the limit"


def test_forget_lets_a_person_be_told_again() -> None:
    notices = ColdDmNotices()
    notices.first_time("a")
    notices.forget("a")
    assert notices.first_time("a") is True
