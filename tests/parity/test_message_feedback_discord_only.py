"""Executable record of message feedback's per-platform capture mechanisms.

Discord captures votes through seeded reactions; Slack captures them through
buttons on the final answer message, chosen specifically so the bot token
never needs the ``reactions:read`` and ``im:write`` scopes (adding either
forces every installed workspace through re-authorization). Asserting both
halves here, rather than merely documenting them, makes drift fail loudly:
a scope quietly growing without a reason, or the Slack surface disappearing
while the core still claims both platforms are covered.

No platform parametrization, no database -- this is a scope-and-surface
check, not a scenario test.
"""

from __future__ import annotations

import daimon.core.message_feedback
from daimon.adapters.slack.feedback import handle_feedback_vote, run_feedback_text_submission
from daimon.core.slack_oauth import SLACK_BOT_SCOPES


def test_slack_bot_scopes_still_lack_the_reaction_feedback_scopes() -> None:
    assert "reactions:read" not in SLACK_BOT_SCOPES and "im:write" not in SLACK_BOT_SCOPES, (
        "Slack feedback deliberately uses buttons instead of reactions so the bot token "
        "never needs reactions:read or im:write (either scope forces every installed "
        "workspace to re-authorize); if a scope grew, either the button path was replaced "
        "on purpose -- update this record -- or the scope is an accident and must go"
    )


def test_slack_feedback_surface_exists() -> None:
    assert callable(handle_feedback_vote) and callable(run_feedback_text_submission), (
        "the Slack button-based feedback path must exist alongside Discord's reaction path; "
        "if it was removed on purpose, restore the Discord-only exemption this test replaced"
    )


def test_message_feedback_core_documents_the_per_platform_split() -> None:
    doc = daimon.core.message_feedback.__doc__

    assert doc is not None, "daimon.core.message_feedback must carry a module docstring"
    assert "reactions:read" in doc, (
        "the module docstring must name the reactions:read scope the Slack button path avoids"
    )
    assert "im:write" in doc, (
        "the module docstring must name the im:write scope the Slack button path avoids"
    )
    assert "button" in doc.lower(), (
        "the module docstring must describe the Slack button-based capture path"
    )
    assert "Discord-only" not in doc, (
        "the module docstring must not still claim message feedback is Discord-only -- "
        "the Slack button path exists now"
    )
