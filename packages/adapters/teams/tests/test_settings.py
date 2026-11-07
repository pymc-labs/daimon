"""The Teams `settings` command: the card per viewer, and what a press applies."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.teams import settings as teams_settings
from daimon.adapters.teams.card_actions import Actor
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.scope_settings import LevelValue, SettingsPlace, SettingView

CHANNEL = "19:abc@thread.tacv2"
THREAD = f"{CHANNEL};messageid=1700"
TENANT = uuid.uuid4()


def _views() -> tuple[SettingView, ...]:
    def view(key: Any, name: str, levels: tuple[LevelValue, ...], effective: str) -> SettingView:
        return SettingView(
            key=key,
            name=name,
            levels=levels,
            effective=effective,
            effective_label=effective,
            decided_by="default",
            available=True,
            active=True,
            note=f"{name}: {effective}.",
        )

    three = (
        LevelValue("server", "inherit"),
        LevelValue("channel", "inherit"),
        LevelValue("thread", "inherit"),
    )
    return (
        view("approvals", "Approvals", three, "on"),
        view("participation", "Thread participation", three, "off"),
        view("progress_plans", "Progress plans", three, "off"),
        view("cost_footer", "Cost footer", (LevelValue("server", "shown"),), "shown"),
    )


@dataclass
class _Asked:
    kind: str
    channel_id: str
    thread_id: str
    user_id: str = "user-1"


def _context(kind: str, *, admin: bool) -> tuple[Any, list[Any]]:
    sent: list[Any] = []

    async def send(activity: Any) -> Any:
        sent.append(activity)
        return MagicMock()

    async def send_card(card: Any) -> None:
        sent.append(card)

    asked = _Asked(
        kind, CHANNEL if kind == "channel" else "a:chat", THREAD if kind == "channel" else "a:chat"
    )
    context = SimpleNamespace(
        runtime=MagicMock(),
        asked_in=None,
        inbound=asked,
        tenant_id=TENANT,
        is_admin=admin,
        send=send,
        send_card=send_card,
    )
    return context, sent


def _buttons(card: Any) -> list[dict[str, Any]]:
    data = card.model_dump(by_alias=True, exclude_none=True)
    return [a for block in data["body"] if block["type"] == "ActionSet" for a in block["actions"]]


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    read = AsyncMock(return_value=_views())
    monkeypatch.setattr(teams_settings, "read_settings", read)
    monkeypatch.setattr(teams_settings, "actor_account", AsyncMock(return_value=None))
    monkeypatch.setattr(
        teams_settings.SettingsDefaults, "from_settings", MagicMock(return_value=None)
    )
    return read


async def test_an_admin_sees_buttons_for_every_row(
    monkeypatch: pytest.MonkeyPatch, reads: AsyncMock
) -> None:
    monkeypatch.setattr(teams_settings, "may_change_approvals", AsyncMock(return_value=True))
    context, sent = _context("channel", admin=True)
    await teams_settings.show_settings(context)
    place = reads.await_args.kwargs["place"]
    assert place == SettingsPlace("teams", CHANNEL, THREAD), "the post it was typed in"
    keys = {b["data"]["op"].split(":")[0] for b in _buttons(sent[0])}
    assert keys == {"approvals", "participation", "progress_plans", "cost_footer"}
    assert all(b["verb"] == "settings" for b in _buttons(sent[0]))


async def test_a_member_sees_only_a_threads_switches(
    monkeypatch: pytest.MonkeyPatch, reads: AsyncMock
) -> None:
    monkeypatch.setattr(teams_settings, "may_change_approvals", AsyncMock(return_value=False))
    context, sent = _context("channel", admin=False)
    await teams_settings.show_settings(context)
    ops = {b["data"]["op"] for b in _buttons(sent[0])}
    assert ops, "a member may still change this thread's switches"
    assert all(op.split(":")[1] == "thread" for op in ops)
    assert not any(op.startswith(("approvals", "cost_footer")) for op in ops)


async def test_the_one_to_one_chat_is_pointed_to_a_channel(reads: AsyncMock) -> None:
    context, sent = _context("dm", admin=True)
    await teams_settings.show_settings(context)
    reads.assert_not_awaited()
    assert "channel post" in json.dumps(sent[0].model_dump(by_alias=True, exclude_none=True))


def _press(op: str, *, conversation: str = THREAD, kind: str = "channel", **data: str) -> MagicMock:
    ctx = MagicMock()
    ctx.activity.conversation.id = conversation
    ctx.activity.conversation.conversation_type = kind
    ctx.activity.value.action.data = {"action": "settings", "op": op} | data
    return ctx


def _actor(*, admin: bool = False) -> Actor:
    return Actor(
        user_id="clicker",
        tenant_id=TENANT,
        is_admin=admin,
        is_platform_admin=admin,
        conversation_id=THREAD,
    )


@pytest.fixture
def change(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    applied = AsyncMock(
        return_value=SimpleNamespace(note="Progress plans is now On for this thread.")
    )
    monkeypatch.setattr(teams_settings, "change_setting", applied)
    monkeypatch.setattr(teams_settings, "actor_account", AsyncMock(return_value=None))
    return applied


async def test_a_press_applies_to_the_post_it_was_pressed_in(
    monkeypatch: pytest.MonkeyPatch, change: AsyncMock
) -> None:
    monkeypatch.setattr(teams_settings, "card_actor", AsyncMock(return_value=_actor()))
    ctx = _press("progress_plans:thread:on", channel="19:other@thread.tacv2", thread="x")
    response = await teams_settings.SettingsActions(MagicMock()).on_action(ctx)
    kwargs = change.await_args.kwargs
    assert kwargs["place"] == SettingsPlace("teams", CHANNEL, THREAD), "card data is ignored"
    assert (kwargs["key"], kwargs["level"], kwargs["value"]) == ("progress_plans", "thread", "on")
    actor = change.await_args.args[2]
    assert actor.native_user_id == "clicker" and actor.surface == "panel"
    assert actor.tenant_id == TENANT
    assert "now On" in str(response)


async def test_a_refusal_is_shown(monkeypatch: pytest.MonkeyPatch, change: AsyncMock) -> None:
    monkeypatch.setattr(teams_settings, "card_actor", AsyncMock(return_value=_actor()))
    change.side_effect = ChannelChangeRefused(
        "Only an admin can change approvals. Ask an admin.", kind="forbidden"
    )
    response = await teams_settings.SettingsActions(MagicMock()).on_action(
        _press("approvals:server:off")
    )
    assert "Ask an admin" in str(response)


@pytest.mark.parametrize(
    "op", ["approvals:everywhere:off", "nope:server:on", "cost_footer:server:on", ""]
)
async def test_forged_choices_change_nothing(
    monkeypatch: pytest.MonkeyPatch, change: AsyncMock, op: str
) -> None:
    monkeypatch.setattr(teams_settings, "card_actor", AsyncMock(return_value=_actor(admin=True)))
    await teams_settings.SettingsActions(MagicMock()).on_action(_press(op))
    change.assert_not_awaited()


async def test_an_unverified_clicker_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, change: AsyncMock
) -> None:
    monkeypatch.setattr(teams_settings, "card_actor", AsyncMock(return_value=None))
    await teams_settings.SettingsActions(MagicMock()).on_action(_press("progress_plans:thread:on"))
    change.assert_not_awaited()


async def test_a_chat_card_trusts_its_post_only_from_an_admin(
    monkeypatch: pytest.MonkeyPatch, change: AsyncMock
) -> None:
    monkeypatch.setattr(teams_settings, "card_actor", AsyncMock(return_value=_actor()))
    member = _press(
        "participation:thread:on",
        conversation="a:chat",
        kind="personal",
        channel=CHANNEL,
        thread=THREAD,
    )
    await teams_settings.SettingsActions(MagicMock()).on_action(member)
    change.assert_not_awaited()

    monkeypatch.setattr(teams_settings, "card_actor", AsyncMock(return_value=_actor(admin=True)))
    await teams_settings.SettingsActions(MagicMock()).on_action(member)
    assert change.await_args.kwargs["place"] == SettingsPlace("teams", CHANNEL, THREAD)


async def test_a_chat_card_naming_a_thread_of_another_channel_is_ignored(
    monkeypatch: pytest.MonkeyPatch, change: AsyncMock
) -> None:
    monkeypatch.setattr(teams_settings, "card_actor", AsyncMock(return_value=_actor(admin=True)))
    forged = _press(
        "participation:thread:on",
        conversation="a:chat",
        kind="personal",
        channel=CHANNEL,
        thread="19:other@thread.tacv2;messageid=1",
    )
    await teams_settings.SettingsActions(MagicMock()).on_action(forged)
    change.assert_not_awaited()
