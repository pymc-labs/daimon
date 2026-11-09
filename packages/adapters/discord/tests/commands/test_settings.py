"""Discord `/settings`: the shared panel drawn as selects, server-bound, redrawn on each press."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.commands import settings as cog
from daimon.core.scope_settings import SettingsPlace
from daimon.core.settings_panel import PanelOption, PanelScreen, PanelSelect

TENANT = uuid.uuid4()
PLACE = SettingsPlace.of("discord", "C1", "T1")
SCREEN = PanelScreen(
    "Cost footer",
    ("In effect: Shown, the default.", "Channel: Default."),
    (PanelSelect("Change for this channel", (PanelOption("cost_footer:channel:hidden", "Hidden"),)),),
    PanelOption("open:chat", "Back"),
)  # fmt: skip


def _interaction(value: str) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.data = {"values": [value]}
    return interaction


@pytest.fixture
def press(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(cog, "chat_actor", AsyncMock(return_value="presser"))
    monkeypatch.setattr(cog, "tenant_agents", MagicMock(return_value="agents"))
    monkeypatch.setattr(cog, "resolve_tenant_for_interaction", AsyncMock(return_value=TENANT))
    pressed = AsyncMock(return_value=SCREEN)
    monkeypatch.setattr(cog, "press", pressed)
    return pressed


def _args(mock: AsyncMock) -> tuple[object, ...]:
    assert mock.await_args is not None
    return mock.await_args.args


def _view() -> cog.SettingsView:
    return cog.SettingsView(MagicMock(), TENANT, PLACE, SCREEN)


def test_guild_only_and_a_select_per_control_with_a_back_button() -> None:
    assert getattr(cog.SettingsCog, "__discord_app_commands_guild_only__", False) is True
    select, back = _view().children
    assert isinstance(select, discord.ui.Select) and isinstance(back, discord.ui.Button)
    assert [(o.label, o.value) for o in select.options] == [
        ("Hidden", "cost_footer:channel:hidden")
    ]
    assert back.label == "Back"


def test_the_embed_carries_the_lines_and_a_refusal_is_red() -> None:
    assert "Channel: Default." in (cog.screen_embed(SCREEN).description or "")
    refused = PanelScreen("X", ("a",), notice="Ask a server admin.", ok=False)
    embed = cog.screen_embed(refused)
    assert "Ask a server admin." in (embed.description or "") and embed.colour != cog.COLOR_BLURPLE


async def test_a_press_goes_to_the_core_with_the_presser_and_redraws(press: AsyncMock) -> None:
    interaction = _interaction("cost_footer:channel:hidden")
    await _view().change(interaction)
    assert _args(press)[2:5] == ("presser", "cost_footer:channel:hidden", PLACE)
    assert cog.chat_actor.await_args.kwargs["user_id"] == "42"  # type: ignore[attr-defined]
    interaction.edit_original_response.assert_awaited_once()


async def test_back_opens_the_screen_it_names(press: AsyncMock) -> None:
    interaction = _interaction("")
    await _view().children[1].callback(interaction)  # type: ignore[call-arg]
    assert _args(press)[3] == "open:chat"


async def test_a_forged_press_redraws_nothing(press: AsyncMock) -> None:
    press.return_value = None
    interaction = _interaction("cost_footer:server:nope")
    await _view().change(interaction)
    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


async def test_a_control_from_another_server_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, press: AsyncMock
) -> None:
    monkeypatch.setattr(cog, "resolve_tenant_for_interaction", AsyncMock(return_value=uuid.uuid4()))
    interaction = _interaction("cost_footer:channel:hidden")
    await _view().change(interaction)
    press.assert_not_awaited()
    assert interaction.followup.send.await_args.kwargs["embed"].description == cog.OTHER_SERVER
