"""The Teams `settings` command: a card of buttons, and where a press applies."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.teams import settings as teams_settings
from daimon.adapters.teams.card_actions import Actor
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.settings_panel import PanelOption, PanelScreen, PanelSelect

CHANNEL = "19:abc@thread.tacv2"
THREAD = f"{CHANNEL};messageid=1700"
TENANT = uuid.uuid4()
SCREEN = PanelScreen(
    "Thread participation",
    ("In effect: Off, the default.",),
    (PanelSelect("Change for this thread", (PanelOption("participation:thread:on", "On"),)),),
    PanelOption("open:chat", "Back"),
    notice="Done.",
)  # fmt: skip


def _context(kind: str) -> tuple[Any, list[Any]]:
    sent: list[Any] = []
    asked = SimpleNamespace(
        kind=kind,
        channel_id=CHANNEL,
        thread_id=THREAD if kind == "channel" else CHANNEL,
        user_id="u1",
    )
    return SimpleNamespace(
        runtime=MagicMock(), asked_in=None, inbound=asked, tenant_id=TENANT, is_admin=False,
        send=AsyncMock(side_effect=sent.append), send_card=AsyncMock(side_effect=sent.append),
    ), sent  # fmt: skip


async def test_the_card_is_for_the_post_it_was_typed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teams_settings, "chat_actor", AsyncMock(return_value="sender"))
    load = AsyncMock(return_value=SCREEN)
    monkeypatch.setattr(teams_settings, "open_panel", load)
    monkeypatch.setattr(teams_settings, "tenant_agents", MagicMock())
    context, sent = _context("channel")
    await teams_settings.show_settings(context)
    assert load.await_args is not None
    assert load.await_args.args[2:] == ("sender", FeaturePlace.of(TENANT, "teams", CHANNEL, THREAD))
    card = sent[0].model_dump(by_alias=True, exclude_none=True)
    buttons = [
        a for block in card["body"] if block["type"] == "ActionSet" for a in block["actions"]
    ]
    assert [(b["verb"], b["data"]["op"]) for b in buttons] == [
        ("settings", "participation:thread:on"),
        ("settings", "open:chat"),
    ]
    chat, sent = _context("personal")
    await teams_settings.show_settings(chat)
    assert "channel post" in json.dumps(sent[0].model_dump(by_alias=True, exclude_none=True))


def _press(op: str, *, conversation: str = THREAD, kind: str = "channel", **data: str) -> MagicMock:
    ctx = MagicMock()
    ctx.activity.conversation.id = conversation
    ctx.activity.conversation.conversation_type = kind
    ctx.activity.value.action.data = {"action": "settings", "op": op} | data
    return ctx


@pytest.fixture
def change(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The panel's `press`, stubbed: the place and presser it is given are what is checked."""
    pressed = AsyncMock(return_value=SCREEN)
    monkeypatch.setattr(teams_settings, "press", pressed)
    monkeypatch.setattr(teams_settings, "tenant_agents", MagicMock())
    monkeypatch.setattr(teams_settings, "chat_actor", AsyncMock(return_value="clicker"))
    return pressed


async def _on(ctx: MagicMock, monkeypatch: pytest.MonkeyPatch, *, admin: bool | None) -> str:
    actor = (
        None if admin is None else Actor("clicker", TENANT, admin, THREAD, is_platform_admin=admin)
    )
    monkeypatch.setattr(teams_settings, "card_actor", AsyncMock(return_value=actor))
    return str(await teams_settings.SettingsActions(MagicMock()).on_action(ctx))


PRESSES = [
    # (case, press, clicker admin? (None: unverified), applied place or None)
    ("channel card: place from the post, card data ignored", _press("progress_plans:thread:on", channel="19:other@thread.tacv2", thread="x"), False, FeaturePlace.of(TENANT, "teams", CHANNEL, THREAD)),
    ("unverified clicker", _press("progress_plans:thread:on"), None, None),
    ("chat card thread from a member", _press("participation:thread:on", conversation="a:chat", kind="personal", channel=CHANNEL, thread=THREAD), False, None),
    ("chat card thread from an admin", _press("participation:thread:on", conversation="a:chat", kind="personal", channel=CHANNEL, thread=THREAD), True, FeaturePlace.of(TENANT, "teams", CHANNEL, THREAD)),
    ("chat card naming another channel's thread", _press("participation:thread:on", conversation="a:chat", kind="personal", channel=CHANNEL, thread="19:other@thread.tacv2;messageid=1"), True, None),
]  # fmt: skip


@pytest.mark.parametrize(("case", "ctx", "admin", "place"), PRESSES, ids=[p[0] for p in PRESSES])
async def test_where_a_press_applies(
    monkeypatch: pytest.MonkeyPatch, change: AsyncMock, case: str, ctx: MagicMock, admin: bool | None, place: FeaturePlace | None
) -> None:  # fmt: skip
    answer = await _on(ctx, monkeypatch, admin=admin)
    if place is None:
        change.assert_not_awaited()
    else:
        assert change.await_args is not None
        assert change.await_args.args[4] == place and "Thread participation" in answer
        assert change.await_args.args[2] == "clicker"


async def test_a_forged_press_says_it_could_not_be_verified(
    monkeypatch: pytest.MonkeyPatch, change: AsyncMock
) -> None:
    change.return_value = None
    answer = await _on(_press("approvals:everywhere:off"), monkeypatch, admin=True)
    assert teams_settings.CANNOT_VERIFY in answer
