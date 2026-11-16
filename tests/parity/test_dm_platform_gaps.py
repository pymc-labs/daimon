"""Executable record of where DM access differs by platform, and why.

A grant's groups are each platform's own: Discord roles come with the member,
Slack user groups need the `usergroups:read` scope (an existing install must be
reinstalled), and Teams groups are teams the app is installed in, read with the
team roster permission; Entra groups would need tenant-wide consent. A Teams
personal chat routes by the same grants, but keeps its own chat session: the
DM wait, history depth and read-only DM memory settings do not apply to it.
No database -- this is a scope check.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from daimon.core.features.registry import FEATURES
from daimon.core.slack_oauth import SLACK_BOT_SCOPES

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_slack_dm_groups_when_listed_then_need_the_user_groups_scope() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/slack-app-manifest.yaml").read_text())
    assert "usergroups:read" in manifest["oauth_config"]["scopes"]["bot"], "the manifest asks"
    assert "usergroups:read" in SLACK_BOT_SCOPES, "the install link asks, so a reinstall adds it"


def test_teams_dm_groups_when_listed_then_are_installed_teams_only() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    granted = [p["name"] for p in manifest["authorization"]["permissions"]["resourceSpecific"]]
    assert "TeamMember.Read.Group" in granted, "a team's roster is read per installed team"
    assert "GroupMember.Read.All" not in str(manifest), "no tenant-wide group consent"


def test_teams_personal_chats_when_routed_then_keep_their_own_session_settings() -> None:
    assert FEATURES["direct_messages"].on_platform("teams"), "Teams DMs follow the grants"
    for key in ("dm_debounce_seconds", "dm_history_depth", "dm_memory_read_only"):
        assert not FEATURES[key].on_platform("teams"), f"{key} is a DM conversation setting"
