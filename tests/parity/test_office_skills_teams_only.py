"""Executable record of the default agent's Teams-only Office skills.

`defaults/` limits the built-in `docx`, `pptx` and `xlsx` skills to Teams
(`platforms: [teams]`), where an edited file can be saved over the channel's
copy. Slack and Discord keep the agent, its skills and the channel-skill room
exactly as before. Asserting it here makes a platform-limited skill reaching
Slack or Discord fail loudly unless this record is replaced. No database.
"""

from __future__ import annotations

from pathlib import Path

from daimon.core.defaults.loader import load_platform_skills

_DEFAULTS = Path(__file__).resolve().parents[2] / "defaults"


def test_only_teams_gets_platform_limited_default_skills() -> None:
    """Every platform-limited skill in `defaults/` is the Office trio, on Teams only."""
    limited = {
        (agent, ref.skill_id, ref.platforms)
        for agent, refs in load_platform_skills(_DEFAULTS).items()
        for ref in refs
    }
    assert limited == {("daimon", name, ("teams",)) for name in ("docx", "pptx", "xlsx")}, (
        f"platform-limited default skills changed: {sorted(limited)} -- if Slack or Discord "
        "gets one, replace this record rather than deleting it"
    )
