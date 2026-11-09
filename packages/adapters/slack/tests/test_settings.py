"""Slack `/settings`: the panel for its caller only, and picks at the signed envelope's place."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import settings as module
from daimon.adapters.slack.blockkit import ERROR_COLOR
from daimon.core.scope_settings import SettingsPlace
from daimon.core.settings_panel import PanelOption, PanelScreen, PanelSelect

PLACE = SettingsPlace.of("slack", "C1", None)
SCREEN = PanelScreen(
    "Cost footer",
    ("In effect: Shown, the default.",),
    (PanelSelect("Change for this channel", (PanelOption("cost_footer:channel:hidden", "Hidden"),)),),
    PanelOption("open:chat", "Back"),
)  # fmt: skip


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    actor = MagicMock(tenant_id="tenant")
    monkeypatch.setattr(module, "chat_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(module, "tenant_agents", MagicMock(return_value="agents"))
    return client


def _args(mock: Any) -> tuple[Any, ...]:
    assert mock.await_args is not None
    return mock.await_args.args


def _pick(value: str, *, button: bool = False) -> dict[str, Any]:
    ids = {"team": {"id": "T1"}, "channel": {"id": "C1"}, "user": {"id": "U1"}}
    element: dict[str, Any] = {"value": value} if button else {"selected_option": {"value": value}}
    return ids | {"response_url": "https://hooks.example/r", "actions": [{"action_id": module.ACTION_ID} | element]}  # fmt: skip


def test_a_screen_is_a_select_per_control_and_a_back_button() -> None:
    blocks = module.build_settings_blocks(SCREEN)
    elements = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert [e["type"] for e in elements] == ["static_select", "button"]
    assert elements[0]["options"][0]["value"] == "cost_footer:channel:hidden"
    assert elements[1]["value"] == "open:chat"
    assert all(e["action_id"] == module.ACTION_ID for e in elements)
    assert "attachments" not in module.screen_fields(SCREEN)
    refused = PanelScreen("X", ("a",), notice="Ask a workspace admin.", ok=False)
    (card,) = module.screen_fields(refused)["attachments"]
    assert card["color"] == ERROR_COLOR


async def test_the_panel_is_ephemeral_at_the_channel_with_no_thread_level(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock
) -> None:
    opened = AsyncMock(return_value=SCREEN)
    monkeypatch.setattr(module, "open_panel", opened)
    await module.handle_settings_command(
        MagicMock(), {"team_id": "T1", "channel_id": "C1", "user_id": "U1"}
    )
    assert _args(opened)[3] == PLACE
    posted = client.chat_postEphemeral.await_args.kwargs
    assert (posted["channel"], posted["user"]) == ("C1", "U1")
    opened.reset_mock()
    await module.handle_settings_command(
        MagicMock(), {"team_id": "T1", "channel_id": "D1", "user_id": "U1"}
    )
    opened.assert_not_awaited()
    assert "attachments" in client.chat_postEphemeral.await_args.kwargs, "a DM is an error card"


@pytest.mark.parametrize("button", [False, True])
async def test_a_pick_replaces_the_message_at_the_envelope_place(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock, button: bool
) -> None:
    pressed = AsyncMock(return_value=SCREEN)
    monkeypatch.setattr(module, "press", pressed)
    sent = AsyncMock()
    monkeypatch.setattr(
        module, "AsyncWebhookClient", MagicMock(return_value=MagicMock(send_dict=sent))
    )
    await module.handle_settings_change(
        MagicMock(), _pick("cost_footer:channel:hidden", button=button)
    )
    assert _args(pressed)[3:5] == ("cost_footer:channel:hidden", PLACE)
    assert module.chat_actor.await_args.kwargs["user_id"] == "U1"  # type: ignore[attr-defined]
    assert _args(sent)[0]["replace_original"] is True


async def test_a_forged_pick_replaces_nothing(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock
) -> None:
    monkeypatch.setattr(module, "press", AsyncMock(return_value=None))
    sent = AsyncMock()
    monkeypatch.setattr(
        module, "AsyncWebhookClient", MagicMock(return_value=MagicMock(send_dict=sent))
    )
    await module.handle_settings_change(MagicMock(), _pick("budget:channel:on"))
    sent.assert_not_awaited()
    client.chat_postEphemeral.assert_not_awaited()
