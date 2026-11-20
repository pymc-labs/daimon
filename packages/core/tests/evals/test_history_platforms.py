"""A case's history reaches the agent in its own platform's shape, not Slack's."""

from __future__ import annotations

from typing import Any

import pytest
from daimon.core.evals.format import Case
from daimon.core.evals.history import turn_text
from daimon.testing.eval_world import eval_case

_PEOPLE = [
    {"id": "u1", "display_name": "Alex", "is_requester": True, "role": "admin"},
    {"id": "u2", "display_name": "Sam"},
]
_BOT = {"id": "b1", "display_name": "Daimon", "is_bot": True}
_AT = "2026-01-05T09:15:00Z"


def _case(platform: str, conversation: str, **surface: Any) -> Case:  # noqa: ANN401
    return eval_case(
        {},
        surface={"platform": platform, "conversation": conversation, "participants": _PEOPLE,
                 "bot": _BOT, **surface},
        history=[{"id": "m0", "author": "u2", "at": _AT, "text": "Is <b>this</b> new?"},
                 {"id": "m00", "author": "b1", "at": _AT, "text": "Yes", "reply_to": "m0"}],
        turn={"messages": [{"id": "m1", "author": "u1", "at": _AT, "text": "Summarize",
                            "reply_to": "m0"}]},
    )  # fmt: skip


_CHANNEL = {"id": "c1", "name": "general"}


def test_discord_thread_when_rendered_then_ids_places_and_query_follow_discord() -> None:
    text = turn_text(_case("discord", "thread", channel=_CHANNEL, thread={"id": "t1", "name": "T"}))

    assert '<channel platform="discord" id="c1" role="parent_channel" name="general"' in text
    assert '<thread platform="discord" id="t1" role="current_thread"' in text
    assert '<message id="m0" author_name="Sam" user_id="u2" is_bot="false"' in text
    assert 'is_bot="true" timestamp="2026-01-05T09:15:00+00:00" reply_to="m0">Yes' in text
    assert "Is &lt;b&gt;this&lt;/b&gt; new?" in text
    assert '<user_query message_id="m1" channel_id="c1" author_name="Alex" user_id="u1"' in text
    assert 'is_admin="true" reply_to="m0">Summarize</user_query>' in text
    assert "<thread_history " in text


def test_discord_channel_mention_when_rendered_then_channel_context_is_unwrapped() -> None:
    text = turn_text(_case("discord", "channel", channel=_CHANNEL))

    assert '<channel_context count="2" source="discord"' in text
    assert '<thread platform="discord" id="m1" role="current_thread"' in text
    assert "<context>" not in text


def test_discord_merged_request_when_two_messages_then_query_sources_name_each() -> None:
    case = _case("discord", "thread", channel=_CHANNEL, thread={"id": "t1", "name": "T"})
    case.turn.messages.append(case.turn.messages[0].model_copy(update={"id": "m2", "text": "More"}))

    text = turn_text(case)

    assert "Summarize\n\nMore<query_sources>" in text
    assert '<message message_id="m2" channel_id="c1"' not in text  # channel is the source's own
    assert text.count("<message message_id=") == 2


def test_teams_channel_thread_when_rendered_then_places_and_message_ids_follow_teams() -> None:
    text = turn_text(_case("teams", "thread", channel=_CHANNEL, thread={"id": "t1", "name": "T"}))

    assert '<channel platform="teams" id="c1" role="parent_channel" name="general"/>' in text
    assert '<thread platform="teams" id="t1" role="current_thread"/>' in text
    assert '<message author_name="Sam" user_id="u2" is_bot="false" timestamp=' in text
    assert 'is_self="true"' in text
    assert '<user_query author_name="Alex" author_id="u1" timestamp=' in text


def test_teams_channel_mention_when_rendered_then_each_message_names_its_thread() -> None:
    text = turn_text(_case("teams", "channel", channel=_CHANNEL))

    assert '<channel_context source="teams" count="2"' in text
    assert 'thread_id="c1;messageid=m0"' in text
    assert '<thread platform="teams" id="c1;messageid=m1" role="current_thread"/>' in text


def test_teams_one_to_one_when_rendered_then_nothing_is_replayed() -> None:
    text = turn_text(_case("teams", "dm"))

    assert "Is &lt;b&gt;" not in text
    assert "thread_history" not in text
    assert "<user_query " in text


@pytest.mark.parametrize("conversation", ["group_dm"])
def test_teams_group_chat_when_rendered_then_chat_history_in_a_group_place(
    conversation: str,
) -> None:
    text = turn_text(_case("teams", conversation))

    assert 'role="group_chat"' in text
    assert "<chat_history " in text


def test_slack_when_rendered_then_the_slack_shape_is_unchanged() -> None:
    text = turn_text(_case("slack", "thread", channel=_CHANNEL, thread={"id": "t1", "name": "T"}))

    assert '<message user_id="u2" username="Sam" is_bot="false" timestamp=' in text
    assert '<user_query author_id="u1" is_admin="true" author_name="Alex">Summarize' in text
