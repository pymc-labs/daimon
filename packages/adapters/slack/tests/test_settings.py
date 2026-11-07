"""Slack `/settings`: the shared panel for its caller, and picks applied by the core writer."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import settings as module
from daimon.adapters.slack.blockkit import ERROR_COLOR
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.scope_settings import LevelValue, SettingChange, SettingsPlace, SettingView
from daimon.core.settings_card import build_settings_panel, may_change_for

PLACE = SettingsPlace.of("slack", "C1", None)


def _views() -> tuple[SettingView, ...]:
    def view(key: Any, name: str, value: str, levels: tuple[str, ...]) -> SettingView:
        return SettingView(
            key=key,
            name=name,
            levels=tuple(LevelValue(level, "inherit") for level in levels),  # type: ignore[arg-type]
            effective=value,
            effective_label=value,
            decided_by="default",
            available=True,
            active=True,
            note=f"{name}: {value}.",
        )

    return (
        view("approvals", "Approvals", "on", ("server", "channel")),
        view("progress_plans", "Progress plans", "off", ("server", "channel")),
        view("cost_footer", "Cost footer", "shown", ("server",)),
    )


def _selects(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [block["accessory"] for block in blocks if "accessory" in block]


@pytest.mark.parametrize(("admin", "expected"), [(True, 3), (False, 0)])
async def test_panel_shows_controls_to_an_admin_and_none_to_a_member(
    monkeypatch: pytest.MonkeyPatch, admin: bool, expected: int
) -> None:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(module, "read_settings", AsyncMock(return_value=_views()))
    monkeypatch.setattr(module, "resolve_is_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(module, "may_change_approvals", AsyncMock(return_value=admin))
    runtime = MagicMock()

    await module.handle_settings_command(
        runtime, {"team_id": "T1", "channel_id": "C1", "user_id": "U1"}
    )

    kwargs = _args(client.chat_postEphemeral).kwargs
    assert kwargs["channel"] == "C1" and kwargs["user"] == "U1"
    selects = _selects(kwargs["blocks"])
    assert len(selects) == expected
    for select in selects:
        assert select["action_id"] == module.ACTION_ID
        assert all(":thread:" not in o["value"] for o in select["options"])


async def test_settings_in_a_dm_is_an_error_card(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    reader = AsyncMock()
    monkeypatch.setattr(module, "read_settings", reader)

    await module.handle_settings_command(
        MagicMock(), {"team_id": "T1", "channel_id": "D1", "user_id": "U1"}
    )

    reader.assert_not_awaited()
    kwargs = _args(client.chat_postEphemeral).kwargs
    assert "attachments" in kwargs and "blocks" not in kwargs


def _pick(value: str) -> dict[str, Any]:
    return {
        "team": {"id": "T1"},
        "channel": {"id": "C1"},
        "user": {"id": "U1"},
        "actions": [{"action_id": module.ACTION_ID, "selected_option": {"value": value}}],
    }


def _wire(monkeypatch: pytest.MonkeyPatch, change: AsyncMock) -> MagicMock:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(module, "change_setting", change)
    monkeypatch.setattr(module, "_account_id", AsyncMock(return_value=None))
    return client


async def test_a_pick_changes_the_envelope_place_as_the_clicker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _views()[1]
    change = AsyncMock(
        return_value=SettingChange(
            key="progress_plans",
            level="channel",
            before="inherit",
            after="on",
            view=view,
            note="Progress plans is now On for this channel.",
        )
    )
    client = _wire(monkeypatch, change)
    is_admin = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "resolve_is_admin", is_admin)

    await module.handle_settings_change(MagicMock(), _pick("progress_plans:channel:on"))

    kwargs = _args(change).kwargs
    assert kwargs["key"] == "progress_plans" and kwargs["level"] == "channel"
    assert kwargs["value"] == "on" and kwargs["place"] == PLACE
    actor = _args(change).args[2]
    assert actor.native_user_id == "U1" and actor.surface == "panel"
    assert actor.tenant_id != uuid.UUID(int=0)
    assert await actor.admin_for(None) is True
    assert _args(is_admin).kwargs["user_id"] == "U1"
    posted = _args(client.chat_postEphemeral).kwargs
    assert "now On" in posted["attachments"][0]["fallback"]


async def test_a_refusal_is_an_error_card(monkeypatch: pytest.MonkeyPatch) -> None:
    change = AsyncMock(
        side_effect=ChannelChangeRefused(
            "Only a workspace admin can change approvals.", kind="forbidden"
        )
    )
    client = _wire(monkeypatch, change)

    await module.handle_settings_change(MagicMock(), _pick("approvals:server:off"))

    attachment = _args(client.chat_postEphemeral).kwargs["attachments"][0]
    assert "Only a workspace admin" in attachment["fallback"]
    assert attachment["color"] == ERROR_COLOR


@pytest.mark.parametrize(
    "value", ["approvals:server:maybe", "budget:channel:on", "approvals:galaxy:on", "x", ""]
)
async def test_a_forged_choice_is_ignored(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    change = AsyncMock()
    client = _wire(monkeypatch, change)

    await module.handle_settings_change(MagicMock(), _pick(value))

    change.assert_not_awaited()
    client.chat_postEphemeral.assert_not_awaited()


def test_blocks_carry_every_row_and_its_choices() -> None:
    panel = build_settings_panel(
        _views(), place=PLACE, may_change=may_change_for(admin=True, native_admin=True)
    )
    blocks = module.build_settings_blocks(panel)
    assert len(blocks) == 1 + len(panel.rows)
    values = {o["value"] for s in _selects(blocks) for o in s["options"]}
    assert "approvals:server:off" in values and "cost_footer:server:hidden" in values


def _args(mock: AsyncMock) -> Any:
    assert mock.await_args is not None
    return mock.await_args
