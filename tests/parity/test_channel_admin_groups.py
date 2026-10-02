"""Executable record of channel admin groups on each platform.

A grant's group ids are Discord roles, Slack user groups and Teams teams (whose
owners count). Discord sends a member's roles with each event; Slack and Teams
look up only grant-named groups, so each has its own lookup module and both
store what a turn matched. Outside a turn the stored Slack and Teams groups are
looked up again; Discord's stored roles stand. No platform parametrization, no
database.
"""

from __future__ import annotations

from pathlib import Path

import daimon.adapters.slack
import daimon.adapters.teams
import daimon.core.channel_admins
import pytest
from daimon.adapters.slack.agent_setup.panel_views import build_channel_admins_form
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.core.channel_admins import (
    LOOKED_UP_GROUP_PLATFORMS,
    InvalidChannelAdminIds,
    confirm_stored_group_ids,
    normalize_channel_admin_ids,
)

_TEAM = "0f2c8a51-7d3e-4b9a-8c61-2e5f4a9b7c10"


@pytest.mark.parametrize(
    ("platform", "channel_id", "group_id"),
    [
        ("discord", "123456789012345678", "223456789012345678"),
        ("slack", "C0GROWTH", "S0LEADS"),
        ("teams", "19:abc@thread.tacv2", _TEAM.upper()),
    ],
)
def test_each_platform_takes_its_own_group_ids(
    platform: str, channel_id: str, group_id: str
) -> None:
    _, groups, _ = normalize_channel_admin_ids(
        platform, channel_id=channel_id, role_ids=[group_id], user_ids=[]
    )
    assert groups == (group_id.lower() if platform == "teams" else group_id,), (
        "a group id is stored, a Teams one lower-cased like an Entra object id"
    )


@pytest.mark.parametrize(
    ("platform", "channel_id", "foreign_id"),
    [
        ("slack", "C0GROWTH", "223456789012345678"),
        ("teams", "19:abc@thread.tacv2", "S0LEADS"),
        ("discord", "123456789012345678", "S0LEADS"),
    ],
)
def test_another_platforms_group_id_is_refused(
    platform: str, channel_id: str, foreign_id: str
) -> None:
    with pytest.raises(InvalidChannelAdminIds, match="invalid"):
        normalize_channel_admin_ids(
            platform, channel_id=channel_id, role_ids=[foreign_id], user_ids=[]
        )


def test_slack_and_teams_store_the_groups_a_turn_matched() -> None:
    for package, module in (
        (daimon.adapters.slack, "channel_admin_groups"),
        (daimon.adapters.teams, "channel_admin_groups"),
    ):
        root = Path(package.__file__).parent
        assert (root / f"{module}.py").is_file(), f"{package.__name__} has its group lookup"
        assert "platform_role_ids" in (root / "app.py").read_text(), (
            f"{package.__name__} admission records the matched groups"
        )


async def test_only_discord_trusts_stored_groups_without_a_lookup() -> None:
    """Slack lets members edit user groups by default; Discord guards roles with Manage Roles."""
    assert {"slack", "teams"} == LOOKED_UP_GROUP_PLATFORMS
    for platform, stands in (("discord", True), ("slack", False), ("teams", False)):
        kept = await confirm_stored_group_ids(platform, "u1", ["g1"], None)
        assert bool(kept) is stands, f"{platform}: a stored group with no lookup"


def test_slack_channel_admins_form_offers_members_and_groups() -> None:
    form = build_channel_admins_form(
        meta=PanelMetadata(team_id="T1", channel_id="C0GROWTH", view="channel_admins"),
        user_ids=[],
        groups={"S0LEADS": "@leads (Leads)"},
    )
    selects = [block["element"]["type"] for block in form["blocks"] if block["type"] == "input"]
    assert selects == ["multi_users_select", "multi_static_select"], "members, then groups"


def test_core_documents_the_record() -> None:
    doc = daimon.core.channel_admins.__doc__
    assert doc is not None and "test_channel_admin_groups" in doc, "core names the parity record"
