"""Executable record of channel admin roles' deliberate Discord-only scope.

Slack has no member roles, so a Slack channel admin is named by user id only:
the ids refuse roles, the Slack adapter never stores or sends any, and its
form offers members alone. No platform parametrization, no database.
"""

from __future__ import annotations

from pathlib import Path

import daimon.adapters.slack
import daimon.core.channel_admins
import pytest
from daimon.adapters.slack.agent_setup.panel_views import build_channel_admins_form
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids


def test_slack_channel_admin_ids_refuse_roles() -> None:
    with pytest.raises(InvalidChannelAdminIds, match="no roles"):
        normalize_channel_admin_ids("slack", channel_id="C0GROWTH", role_ids=["S1"], user_ids=[])


def test_slack_adapter_never_stores_member_roles() -> None:
    slack_root = Path(daimon.adapters.slack.__file__).parent
    offenders = sorted(
        str(path.relative_to(slack_root))
        for path in slack_root.rglob("*.py")
        if "platform_role_ids" in path.read_text()
    )
    assert offenders == [], (
        f"Slack adapter files pass member role ids: {offenders} -- if Slack gained roles, "
        "replace this record rather than deleting it"
    )


def test_slack_channel_admins_form_offers_members_only() -> None:
    form = build_channel_admins_form(
        meta=PanelMetadata(team_id="T1", channel_id="C0GROWTH", view="channel_admins"),
        user_ids=[],
    )
    selects = [block["element"]["type"] for block in form["blocks"] if block["type"] == "input"]
    assert selects == ["multi_users_select"]


def test_core_documents_the_discord_only_roles() -> None:
    doc = daimon.core.channel_admins.__doc__
    assert doc is not None and "test_channel_admin_roles_discord_only" in doc
