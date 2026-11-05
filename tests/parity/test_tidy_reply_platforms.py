"""delete_reply takes each platform's delete_message rules, part by part.

Discord and Teams refuse a turn's reply while the turn runs, or unless the
person asking started it or is an admin (on Discord also whoever opened the
thread). Slack refuses a running turn's reply but has no requester rule, for
one message or a whole reply alike
(`test_anyone_may_delete_a_finished_turns_reply_there_is_no_requester_rule`).
Slack also never records a routine's result, so delete_reply cannot take one
there. No database: this is a scope check; each platform's own tidy tests drive
the behaviour.
"""

from __future__ import annotations

from pathlib import Path

from daimon.adapters.mcp.tools import _tidy as tidy
from daimon.adapters.mcp.tools.discord import _tidy as discord_tidy
from daimon.adapters.mcp.tools.slack import _tidy as slack_tidy
from daimon.adapters.mcp.tools.teams import _tidy as teams_tidy

_ADAPTERS = Path(__file__).resolve().parents[2] / "packages" / "adapters"


def test_only_slack_applies_no_requester_rule() -> None:
    for module in (discord_tidy, teams_tidy):
        assert module.require_conversation_rights is tidy.require_conversation_rights, (
            f"{module.__name__} checks who may tidy a turn's reply"
        )
    assert not hasattr(slack_tidy, "require_conversation_rights"), (
        "Slack tidy applies no requester rule"
    )


def test_every_platform_refuses_a_running_turn_and_deletes_through_one_loop() -> None:
    assert slack_tidy.require_turns_over is tidy.require_turns_over, (
        "Slack has no requester rule but still refuses a running turn"
    )
    for module in (discord_tidy, slack_tidy, teams_tidy):
        assert module.delete_parts is tidy.delete_parts, (
            f"{module.__name__} deletes a reply through the shared per-part loop"
        )


def test_slack_never_records_a_routine_result_so_delete_reply_cannot_take_one() -> None:
    """A routine's result is a `source='routine'` post only where its adapter writes one."""
    writers = {
        adapter
        for adapter in ("discord", "slack", "teams")
        for path in (_ADAPTERS / adapter / "daimon" / "adapters" / adapter).rglob("*.py")
        if 'source="routine"' in path.read_text()
    }
    assert writers == {"discord", "teams"}, "only Discord and Teams record a routine's result"
