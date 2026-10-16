"""Teams thread keys."""

from __future__ import annotations

import pytest
from daimon.core.teams_threads import is_group_chat


@pytest.mark.parametrize(
    ("conversation", "group"),
    [
        ("19:abc@thread.v2", True),
        ("19:meeting_xyz@thread.v2;messageid=1", True),
        ("19:abc@thread.tacv2", False),
        ("19:abc@thread.tacv2;messageid=1", False),
        ("19:abc@thread.skype", False),
        ("a:1xyz", False),
    ],
)
def test_is_group_chat_tells_chats_from_channels_and_1_1(conversation: str, group: bool) -> None:
    assert is_group_chat(conversation) is group
