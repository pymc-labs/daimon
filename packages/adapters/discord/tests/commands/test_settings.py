"""Discord `/settings`: one select per row, server-bound controls, presses go to the core."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.commands import settings as cog
from daimon.core.scope_settings import SettingsPanel, SettingsPlace, SettingsRow

TENANT = uuid.uuid4()
PLACE = SettingsPlace.of("discord", "C1", "T1")
PANEL = SettingsPanel("Settings", "Pick.", (
    SettingsRow("approvals", "Approvals", "Ask.", (), "Change approvals"),
    SettingsRow("participation", "Thread participation", "Off.", (("participation:thread:on", "Thread: On"),), "Change"),
))  # fmt: skip


def _interaction(value: str) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer, interaction.followup.send = AsyncMock(), AsyncMock()
    interaction.data = {"values": [value]}
    return interaction


@pytest.fixture
def apply(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(cog, "chat_actor", AsyncMock(return_value="presser"))
    applied = AsyncMock(return_value=(False, "Only a server admin can change approvals."))
    monkeypatch.setattr(cog, "apply_choice", applied)
    return applied


def test_guild_only_and_a_select_only_for_rows_with_choices() -> None:
    assert getattr(cog.SettingsCog, "__discord_app_commands_guild_only__", False) is True
    (select,) = cog.SettingsView(MagicMock(), TENANT, PLACE, PANEL).children
    assert isinstance(select, discord.ui.Select)
    assert [(o.label, o.value) for o in select.options] == [
        ("Thread: On", "participation:thread:on")
    ]


async def test_a_press_goes_to_the_core_with_the_presser_and_a_refusal_is_an_error_card(
    monkeypatch: pytest.MonkeyPatch, apply: AsyncMock
) -> None:
    monkeypatch.setattr(cog, "resolve_tenant_for_interaction", AsyncMock(return_value=TENANT))
    interaction = _interaction("approvals:thread:off")
    await cog.SettingsView(MagicMock(), TENANT, PLACE, PANEL).change(interaction)
    assert apply.await_args.args[2:] == ("presser", "approvals:thread:off", PLACE)
    assert cog.chat_actor.await_args.kwargs["user_id"] == "42"  # type: ignore[attr-defined]
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "Only a server admin" in embed.description and embed.colour != discord.Colour.blurple()
    apply.return_value = None
    forged = _interaction("approvals:server:disabled")
    await cog.SettingsView(MagicMock(), TENANT, PLACE, PANEL).change(forged)
    forged.followup.send.assert_not_awaited()


async def test_a_control_from_another_server_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, apply: AsyncMock
) -> None:
    monkeypatch.setattr(cog, "resolve_tenant_for_interaction", AsyncMock(return_value=uuid.uuid4()))
    interaction = _interaction("approvals:server:off")
    await cog.SettingsView(MagicMock(), TENANT, PLACE, PANEL).change(interaction)
    apply.assert_not_awaited()
    assert interaction.followup.send.await_args.kwargs["embed"].description == cog.OTHER_SERVER
