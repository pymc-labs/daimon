"""Executable record: the Add skill form takes a file on Discord only.

Discord modals carry a file input, so its form takes a pasted SKILL.md (up to
4,000 characters) or one .md or .zip. Slack modals carry no file input, so its
form takes a paste (up to 3,000 characters) and points files to chat, where
add_skill runs the same checks on both platforms. Chat adds only through a
confirmation card, so without tool safety a Slack file has no way in. If Slack
gains file inputs, update this record rather than deleting it.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import discord
from daimon.adapters.discord.agent_setup.add_skill import AddSkillModal
from daimon.adapters.slack.agent_setup.panel_views import (
    MAX_SKILL_PASTE_CHARS,
    build_add_skill_form,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.core.roster import RosterAgent


def _discord_inputs() -> list[Any]:
    agent = RosterAgent(name="helper", ma_agent_id="ag_1", model_id="m", is_built_in=False)
    modal = AddSkillModal(cast(Any, MagicMock()), agent)
    return [child.component for child in modal.children if isinstance(child, discord.ui.Label)]


def test_discord_takes_a_paste_or_a_file() -> None:
    paste, upload = _discord_inputs()
    assert isinstance(paste, discord.ui.TextInput) and paste.max_length == 4000
    assert isinstance(upload, discord.ui.FileUpload)


def test_slack_takes_a_paste_and_points_files_to_chat_only_where_chat_can_confirm() -> None:
    meta = PanelMetadata(team_id="T1", channel_id="C1", view="add_skill", agent_name="helper")
    form = build_add_skill_form(meta=meta)
    inputs = [block["element"] for block in form["blocks"] if block["type"] == "input"]
    assert [element["type"] for element in inputs] == ["plain_text_input"]
    assert inputs[0]["max_length"] == MAX_SKILL_PASTE_CHARS == 3000
    assert "attach it in a message" in str(form["blocks"])
    no_card = str(build_add_skill_form(meta=meta, files_in_chat=False)["blocks"])
    assert "attach it" not in no_card and "paste the SKILL.md" in no_card
