"""Executable record of the default agent's Teams-only skills.

`defaults/` limits the built-in `docx`, `pptx` and `xlsx` skills to Teams
(`platforms: [teams]`), where an edited file can be saved over the channel's
copy, and `microsoft-365`, whose per-person tools exist only on Teams
(`test_microsoft_accounts_teams_only.py`). `workflow-authoring` is limited to the chat apps and the web
app, where a workflow is authored. Slack and Discord carry only that one added skill and keep the agent and the channel-skill room
exactly as before. Asserting it here makes a platform-limited skill reaching
Slack or Discord fail loudly unless this record is replaced. No database.
"""

from __future__ import annotations

from pathlib import Path

from daimon.core.defaults.loader import load_platform_skills

_DEFAULTS = Path(__file__).resolve().parents[2] / "defaults"


def test_only_teams_gets_platform_limited_default_skills() -> None:
    """Every platform-limited skill in `defaults/` is Office or Microsoft 365 on Teams only,
    or workflow authoring on every chat platform."""
    limited = {
        (agent, ref.skill_id, ref.platforms)
        for agent, refs in load_platform_skills(_DEFAULTS).items()
        for ref in refs
    }
    names = ("docx", "pptx", "xlsx", "microsoft-365")
    expected = {("default", name, ("teams",)) for name in names}
    expected.add(("default", "workflow-authoring", ("discord", "slack", "teams", "web")))
    assert limited == expected, (
        f"platform-limited default skills changed: {sorted(limited)} -- if Slack or Discord "
        "gets one, replace this record rather than deleting it"
    )
