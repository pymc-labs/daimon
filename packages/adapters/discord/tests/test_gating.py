"""Pre-DB message gate: who may start a turn by mention."""

from __future__ import annotations

from daimon.adapters.discord.gating import is_auto_respond_candidate, should_process_message

DAIMON_ID = "111"
HUMAN_ID = "222"
QA_BOT_ID = "333"
QA_ADMIN_BOT_ID = "555"
OTHER_BOT_ID = "444"


def test_gate_admits_mentioning_human_in_guild() -> None:
    assert should_process_message(
        author_is_bot=False,
        author_id=HUMAN_ID,
        bot_mentioned=True,
        guild_id="g1",
        self_user_id=DAIMON_ID,
    ), "a human's explicit mention in a guild is the ordinary turn trigger"


def test_gate_rejects_human_when_not_mentioned() -> None:
    assert not should_process_message(
        author_is_bot=False,
        author_id=HUMAN_ID,
        bot_mentioned=False,
        guild_id="g1",
        self_user_id=DAIMON_ID,
    ), "an unmentioned message must not start a turn"


def test_gate_rejects_human_mention_in_dm() -> None:
    assert not should_process_message(
        author_is_bot=False,
        author_id=HUMAN_ID,
        bot_mentioned=True,
        guild_id=None,
        self_user_id=DAIMON_ID,
    ), "DMs have no tenant, so they must not start a turn"


def test_gate_rejects_bot_author_when_no_qa_bot_configured() -> None:
    assert not should_process_message(
        author_is_bot=True,
        author_id=OTHER_BOT_ID,
        bot_mentioned=True,
        guild_id="g1",
        self_user_id=DAIMON_ID,
        qa_bot_user_ids=(),
    ), "with no allow-list, every bot-authored mention is rejected"


def test_gate_admits_allow_listed_qa_bot() -> None:
    assert should_process_message(
        author_is_bot=True,
        author_id=QA_BOT_ID,
        bot_mentioned=True,
        guild_id="g1",
        self_user_id=DAIMON_ID,
        qa_bot_user_ids=(QA_BOT_ID,),
    ), "the allow-listed QA bot's mention must start a turn like a human's"


def test_gate_rejects_unlisted_bot_when_qa_bot_configured() -> None:
    assert not should_process_message(
        author_is_bot=True,
        author_id=OTHER_BOT_ID,
        bot_mentioned=True,
        guild_id="g1",
        self_user_id=DAIMON_ID,
        qa_bot_user_ids=(QA_BOT_ID,),
    ), "the allow-list admits only listed ids, never bots in general"


def test_gate_rejects_self_authored_mention_when_allow_listed_to_own_id() -> None:
    assert not should_process_message(
        author_is_bot=True,
        author_id=DAIMON_ID,
        bot_mentioned=True,
        guild_id="g1",
        self_user_id=DAIMON_ID,
        qa_bot_user_ids=(DAIMON_ID,),
    ), "allow-listing daimon's own id must not arm an unbounded self-trigger loop"


def test_gate_admits_each_of_two_allow_listed_bots() -> None:
    for author_id in (QA_BOT_ID, QA_ADMIN_BOT_ID):
        assert should_process_message(
            author_is_bot=True,
            author_id=author_id,
            bot_mentioned=True,
            guild_id="g1",
            self_user_id=DAIMON_ID,
            qa_bot_user_ids=(QA_BOT_ID, QA_ADMIN_BOT_ID),
        ), "both an admin and a non-admin driver must be able to start turns"


def test_gate_rejects_self_authored_mention_when_listed_alongside_others() -> None:
    assert not should_process_message(
        author_is_bot=True,
        author_id=DAIMON_ID,
        bot_mentioned=True,
        guild_id="g1",
        self_user_id=DAIMON_ID,
        qa_bot_user_ids=(QA_BOT_ID, DAIMON_ID),
    ), "the self-trigger refusal must not be bypassable by padding the allow-list"


def test_gate_rejects_qa_bot_when_not_mentioned() -> None:
    assert not should_process_message(
        author_is_bot=True,
        author_id=QA_BOT_ID,
        bot_mentioned=False,
        guild_id="g1",
        self_user_id=DAIMON_ID,
        qa_bot_user_ids=(QA_BOT_ID,),
    ), "the allow-list relaxes the bot-author check only, not the mention requirement"


def test_gate_rejects_qa_bot_in_dm() -> None:
    assert not should_process_message(
        author_is_bot=True,
        author_id=QA_BOT_ID,
        bot_mentioned=True,
        guild_id=None,
        self_user_id=DAIMON_ID,
        qa_bot_user_ids=(QA_BOT_ID,),
    ), "the allow-list relaxes the bot-author check only, not the guild requirement"


# --- organic thread participation candidates -------------------------------


def test_auto_candidate_is_an_unmentioned_human_in_a_guild_thread() -> None:
    assert is_auto_respond_candidate(
        available=True, author_is_bot=False, bot_mentioned=False, in_thread=True, guild_id="g1"
    ), "an unmentioned human message in a guild thread is what the feature screens"


def test_auto_candidate_requires_the_deployment_to_allow_it() -> None:
    assert not is_auto_respond_candidate(
        available=False, author_is_bot=False, bot_mentioned=False, in_thread=True, guild_id="g1"
    ), "a disabled deployment must keep today's mention-only behaviour"


def test_auto_candidate_never_a_bot_and_never_a_mention() -> None:
    assert not is_auto_respond_candidate(
        available=True, author_is_bot=True, bot_mentioned=False, in_thread=True, guild_id="g1"
    ), "bots never trigger organic turns, QA allow-list included"
    assert not is_auto_respond_candidate(
        available=True, author_is_bot=False, bot_mentioned=True, in_thread=True, guild_id="g1"
    ), "a mention belongs to the mention path, not this one"


def test_auto_candidate_only_in_guild_threads() -> None:
    assert not is_auto_respond_candidate(
        available=True, author_is_bot=False, bot_mentioned=False, in_thread=False, guild_id="g1"
    ), "top-level channel messages are never screened"
    assert not is_auto_respond_candidate(
        available=True, author_is_bot=False, bot_mentioned=False, in_thread=True, guild_id=None
    ), "no guild means no tenant"
