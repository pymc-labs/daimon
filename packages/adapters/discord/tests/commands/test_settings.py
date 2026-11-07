"""Discord `/settings`: the shared panel, server-bound controls, and the shared writer."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.commands import settings as cog
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.scope_settings import LevelValue, SettingsPlace, SettingView
from daimon.core.settings_card import build_settings_panel, may_change_for

TENANT = uuid.uuid4()
PLACE = SettingsPlace.of("discord", "C1", "T1")


def _views() -> list[SettingView]:
    levels = tuple(LevelValue(level, "inherit") for level in ("server", "channel", "thread"))
    return [
        SettingView(
            "approvals",
            "Approvals",
            levels,
            "on",
            "Ask",
            "default",
            True,
            True,
            "Approvals: Ask, the default.",
        ),
        SettingView(
            "progress_plans",
            "Progress plans",
            levels,
            "off",
            "Off",
            "deployment",
            True,
            True,
            "Progress plans: Off for all of Daimon.",
        ),
    ]


def test_guild_only() -> None:
    assert getattr(cog.SettingsCog, "__discord_app_commands_guild_only__", False) is True


def test_admin_gets_one_select_per_row_and_member_gets_thread_switches_only() -> None:
    admin = build_settings_panel(
        _views(), place=PLACE, may_change=may_change_for(admin=True, native_admin=True)
    )
    view = cog.SettingsView(MagicMock(), TENANT, PLACE, admin)
    assert [type(child) for child in view.children] == [discord.ui.Select, discord.ui.Select]
    member = build_settings_panel(
        _views(), place=PLACE, may_change=may_change_for(admin=False, native_admin=False)
    )
    view = cog.SettingsView(MagicMock(), TENANT, PLACE, member)
    (select,) = view.children
    assert isinstance(select, discord.ui.Select)
    assert {option.label for option in select.options} == {"Thread: On", "Thread: Off"}
    embed = cog.build_settings_embed(member)
    assert [field.name for field in embed.fields] == ["Approvals", "Progress plans"]


def _interaction(value: str) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.data = {"values": [value]}
    return interaction


def _runtime() -> MagicMock:
    runtime = MagicMock()
    session = MagicMock()
    runtime.sessionmaker.return_value.__aenter__ = AsyncMock(return_value=session)
    runtime.sessionmaker.return_value.__aexit__ = AsyncMock(return_value=None)
    return runtime


async def test_a_control_from_another_server_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = AsyncMock()
    monkeypatch.setattr(cog, "change_setting", change)
    monkeypatch.setattr(cog, "resolve_tenant_for_interaction", AsyncMock(return_value=uuid.uuid4()))
    view = cog.SettingsView(
        _runtime(),
        TENANT,
        PLACE,
        build_settings_panel(
            _views(), place=PLACE, may_change=may_change_for(admin=True, native_admin=True)
        ),
    )
    interaction = _interaction("approvals:server:off")
    await view.change(interaction)
    change.assert_not_awaited()
    assert interaction.followup.send.await_args.kwargs["embed"].description == cog.OTHER_SERVER


async def test_a_press_goes_to_the_shared_writer_with_the_presser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = MagicMock(note="Approvals is now Allow writes for this thread.")
    change = AsyncMock(return_value=result)
    monkeypatch.setattr(cog, "change_setting", change)
    monkeypatch.setattr(cog, "actor_account", AsyncMock(return_value=None))
    monkeypatch.setattr(cog, "resolve_tenant_for_interaction", AsyncMock(return_value=TENANT))
    view = cog.SettingsView(
        _runtime(),
        TENANT,
        PLACE,
        build_settings_panel(
            _views(), place=PLACE, may_change=may_change_for(admin=True, native_admin=True)
        ),
    )
    interaction = _interaction("approvals:thread:off")
    await view.change(interaction)
    kwargs = change.await_args.kwargs
    assert (kwargs["key"], kwargs["level"], kwargs["value"]) == ("approvals", "thread", "off")
    assert kwargs["place"] == PLACE
    actor = change.await_args.args[2]
    assert actor.native_user_id == "42" and actor.surface == "panel"
    assert interaction.followup.send.await_args.kwargs["embed"].description == result.note


async def test_a_refusal_is_an_error_card_and_a_forgery_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = AsyncMock(
        side_effect=ChannelChangeRefused(
            "Only a server admin can change approvals. Ask a server admin.", kind="forbidden"
        )
    )
    monkeypatch.setattr(cog, "change_setting", change)
    monkeypatch.setattr(cog, "actor_account", AsyncMock(return_value=None))
    monkeypatch.setattr(cog, "resolve_tenant_for_interaction", AsyncMock(return_value=TENANT))
    view = cog.SettingsView(
        _runtime(),
        TENANT,
        PLACE,
        build_settings_panel(
            _views(), place=PLACE, may_change=may_change_for(admin=True, native_admin=True)
        ),
    )
    refused = _interaction("approvals:server:off")
    await view.change(refused)
    embed = refused.followup.send.await_args.kwargs["embed"]
    assert "Ask a server admin" in embed.description
    assert embed.colour != discord.Colour.blurple()
    forged = _interaction("approvals:server:disabled")
    await view.change(forged)
    assert change.await_count == 1
    forged.followup.send.assert_not_awaited()
