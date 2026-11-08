"""Slack `/settings`: the panel for its caller only, and picks at the signed envelope's place."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import settings as module
from daimon.adapters.slack.blockkit import ERROR_COLOR
from daimon.core import scope_settings as core
from daimon.core.scope_settings import SettingsPanel, SettingsPlace, SettingsRow

PLACE = SettingsPlace.of("slack", "C1", None)
PANEL = SettingsPanel("Settings", "Pick.", (
    SettingsRow("approvals", "Approvals", "Ask.", (("approvals:server:off", "Workspace: Allow writes"),), "Change"),
    SettingsRow("cost_footer", "Cost footer", "Shown.", (), "Change"),
))  # fmt: skip


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(module, "chat_actor", AsyncMock(return_value="clicker"))
    return client


def _pick(value: str) -> dict[str, Any]:
    ids = {"team": {"id": "T1"}, "channel": {"id": "C1"}, "user": {"id": "U1"}}
    return ids | {"actions": [{"action_id": module.ACTION_ID, "selected_option": {"value": value}}]}


async def test_the_panel_is_ephemeral_with_a_select_per_changeable_row(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock
) -> None:
    load = AsyncMock(return_value=PANEL)
    monkeypatch.setattr(module, "load_panel", load)
    await module.handle_settings_command(
        MagicMock(), {"team_id": "T1", "channel_id": "C1", "user_id": "U1"}
    )
    assert load.await_args.args[2:] == ("clicker", PLACE), "no thread level on Slack"
    posted = client.chat_postEphemeral.await_args.kwargs
    assert (posted["channel"], posted["user"]) == ("C1", "U1")
    (select,) = [block["accessory"] for block in posted["blocks"] if "accessory" in block]
    assert select["action_id"] == module.ACTION_ID
    assert [o["value"] for o in select["options"]] == ["approvals:server:off"]
    load.reset_mock()
    await module.handle_settings_command(
        MagicMock(), {"team_id": "T1", "channel_id": "D1", "user_id": "U1"}
    )
    load.assert_not_awaited()
    assert "attachments" in client.chat_postEphemeral.await_args.kwargs, "a DM is an error card"


async def test_a_pick_applies_at_the_envelope_place_and_a_refusal_is_an_error_card(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock
) -> None:
    apply = AsyncMock(return_value=(False, "Only a workspace admin can change approvals."))
    monkeypatch.setattr(module, "apply_choice", apply)
    await module.handle_settings_change(MagicMock(), _pick("approvals:server:off"))
    assert apply.await_args.args[2:] == ("clicker", "approvals:server:off", PLACE)
    assert module.chat_actor.await_args.kwargs["user_id"] == "U1"  # type: ignore[attr-defined]
    attachment = client.chat_postEphemeral.await_args.kwargs["attachments"][0]
    assert "Only a workspace admin" in attachment["fallback"] and attachment["color"] == ERROR_COLOR


@pytest.mark.parametrize(
    "value", ["approvals:server:maybe", "budget:channel:on", "approvals:thread:on:x", ""]
)
async def test_a_forged_pick_changes_and_says_nothing(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock, value: str
) -> None:
    change = AsyncMock()
    monkeypatch.setattr(core, "change_setting", change)
    await module.handle_settings_change(MagicMock(), _pick(value))
    change.assert_not_awaited()
    client.chat_postEphemeral.assert_not_awaited()
