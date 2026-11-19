"""Teams: the origin is the conversation a click came from, never a channel it names."""

from __future__ import annotations

import uuid

import pytest
from daimon.adapters.teams.card_actions import Actor


def _actor(conversation: str) -> Actor:
    return Actor("u1", uuid.uuid4(), True, conversation, True)


@pytest.mark.parametrize(
    ("conversation", "expected"),
    [
        ("19:abc@thread.tacv2", {"19:abc@thread.tacv2"}),
        ("19:abc@thread.tacv2;messageid=1700", {"19:abc@thread.tacv2"}),
        ("a:1personal-chat", set[str]()),
        ("", set[str]()),
    ],
    ids=["channel", "thread", "one-to-one", "none"],
)
def test_the_origin_is_the_clicks_own_conversation(conversation: str, expected: set[str]) -> None:
    assert set(_actor(conversation).origin) >= expected
    if not expected:
        assert not _actor(conversation).origin


def test_a_channel_the_admin_names_is_not_the_origin() -> None:
    assert "19:target@thread.tacv2" not in _actor("a:1personal-chat").origin
