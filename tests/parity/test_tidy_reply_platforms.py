"""delete_reply takes each platform's delete_message rules, part by part.

Discord and Teams refuse a turn's reply while the turn runs, or unless the
person asking started it or is an admin (on Discord also whoever opened the
thread). Slack tidy has no such conversation rule, for one message or a
whole reply alike. No database: this is a scope check.
"""

from __future__ import annotations

import inspect

from daimon.adapters.mcp.tools.discord import _tidy as discord_tidy
from daimon.adapters.mcp.tools.slack import _tidy as slack_tidy
from daimon.adapters.mcp.tools.teams import _tidy as teams_tidy


def test_slack_delete_reply_has_no_conversation_rule() -> None:
    for module in (discord_tidy, teams_tidy):
        assert "require_conversation_rights" in inspect.getsource(module), (
            f"{module.__name__} checks who may tidy a turn's reply"
        )
    assert "require_conversation_rights" not in inspect.getsource(slack_tidy), (
        "Slack tidy applies no turn-running or requester rule"
    )


def test_delete_reply_deletes_each_part_as_delete_message_does() -> None:
    for message, reply in [
        (discord_tidy._delete_message_impl, discord_tidy._delete_reply_impl),  # pyright: ignore[reportPrivateUsage]
        (slack_tidy._slack_delete_message_impl, slack_tidy._slack_delete_reply_impl),  # pyright: ignore[reportPrivateUsage]
        (teams_tidy._teams_delete_message_impl, teams_tidy._teams_delete_reply_impl),  # pyright: ignore[reportPrivateUsage]
    ]:
        assert "_delete_one(" in inspect.getsource(message), f"{message.__name__} deletes one part"
        assert "_delete_one(" in inspect.getsource(reply), (
            f"{reply.__name__} deletes through the same single-message path"
        )
