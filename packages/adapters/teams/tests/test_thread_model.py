"""Teams' `model` command: the choices, then a pick that applies at once."""

from __future__ import annotations

import dataclasses
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.teams import thread_model as module
from daimon.adapters.teams.card_actions import Actor
from daimon.core.thread_model_flow import ThreadOption

CHANNEL = "19:abc@thread.tacv2"
THREAD = f"{CHANNEL};messageid=1700"
TENANT = uuid.uuid4()
KEPT = "This conversation runs on GPT-6. Off there: memory."
OPTIONS = (ThreadOption("GPT-6 ($2)", "openai:gpt-6"), ThreadOption("Agent default", "default"))


def _dump(value: Any) -> dict[str, Any]:
    return json.loads(json.dumps(value.model_dump(by_alias=True, exclude_none=True), default=str))


def _buttons(card: Any) -> list[dict[str, Any]]:
    body = _dump(card).get("body", [])
    return [a for block in body if block["type"] == "ActionSet" for a in block["actions"]]


def _text(card: Any) -> str:
    return json.dumps(_dump(card))


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The membership check and the shared flow, each replaced."""
    seams = SimpleNamespace(
        may=AsyncMock(return_value=True),
        pick=AsyncMock(return_value=KEPT),
        confirm=AsyncMock(return_value=KEPT),
    )
    monkeypatch.setattr(module, "may_choose_thread_model", seams.may)
    monkeypatch.setattr(module, "build_thread_pick", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(module, "thread_options", AsyncMock(return_value=OPTIONS))
    monkeypatch.setattr(module, "pick_thread_model", seams.pick)
    monkeypatch.setattr(module, "confirm_thread_pick", seams.confirm)
    return seams


def _context(*, asked_in: str | None = None) -> tuple[Any, list[Any]]:
    sent: list[Any] = []
    inbound = SimpleNamespace(
        kind="channel", channel_id=CHANNEL, conversation_id=THREAD, user_id="u1"
    )
    return SimpleNamespace(
        runtime=MagicMock(), asked_in=asked_in, inbound=inbound, tenant_id=TENANT, is_admin=False,
        send=AsyncMock(side_effect=sent.append), send_card=AsyncMock(side_effect=sent.append),
    ), sent  # fmt: skip


async def test_the_command_offers_each_choice_as_a_button(flow: SimpleNamespace) -> None:
    """Every choice is one button; the right to choose is checked for this conversation."""
    context, sent = _context()
    await module.show_model(context)
    assert [(b["verb"], b["data"]["op"]) for b in _buttons(sent[0])] == [
        ("thread_model", "pick:openai:gpt-6"),
        ("thread_model", "pick:default"),
    ], "one button per choice"
    assert flow.may.await_args.kwargs["thread_id"] == THREAD, "checked for this conversation"


async def test_a_sender_without_the_right_gets_one_line_naming_who_can_help(
    flow: SimpleNamespace,
) -> None:
    """No buttons for someone who may not choose."""
    flow.may.return_value = False
    context, sent = _context()
    await module.show_model(context)
    assert module.NOT_ALLOWED in _text(sent[0]), "names who can help"
    assert not _buttons(sent[0]), "no buttons"


async def test_a_command_moved_to_the_chat_points_back_to_the_conversation(
    flow: SimpleNamespace,
) -> None:
    """A command answered in the person's chat points them back to the conversation."""
    context, sent = _context(asked_in=THREAD)
    await module.show_model(context)
    assert module.IN_THE_CONVERSATION in _text(sent[0]), "points back"
    flow.may.assert_not_awaited()


def _press(op: str, *, conversation: str = THREAD, kind: str = "channel", **data: Any) -> MagicMock:
    ctx = MagicMock()
    ctx.activity.conversation.id = conversation
    ctx.activity.conversation.conversation_type = kind
    ctx.activity.value.action.data = {"action": "thread_model", "op": op} | data
    return ctx


async def _on(ctx: MagicMock, monkeypatch: pytest.MonkeyPatch, *, actor: Actor | None) -> Any:
    monkeypatch.setattr(module, "card_actor", AsyncMock(return_value=actor))
    return await module.ThreadModelActions(MagicMock()).on_action(ctx)


def _member(**kw: Any) -> Actor:
    return Actor("u1", TENANT, False, THREAD, **kw)


async def test_a_cross_provider_pick_is_kept_at_once_with_no_buttons(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pick on another provider is kept in one press: the note, never a Confirm."""
    answer = await _on(_press("pick:openai:gpt-6"), monkeypatch, actor=_member())
    assert not _buttons(answer.value), "nothing to confirm"
    assert KEPT in _text(answer.value), "the core note, unchanged"
    kept = flow.pick.await_args.kwargs
    assert (kept["conversation_id"], kept["choice"]) == (THREAD, "openai:gpt-6"), "for this thread"


async def test_switch_on_an_old_card_keeps_its_pick_and_ignores_its_digest(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Switch posted before picks applied at once keeps its pick as a new pick would."""
    ctx = _press("confirm", v="openai:gpt-6", d="digest-1", r=3)
    answer = await _on(ctx, monkeypatch, actor=_member())
    assert KEPT in _text(answer.value), "the note replaces the card"
    assert flow.confirm.await_args.args[1] == "openai:gpt-6", "only the pick is read"
    assert flow.confirm.await_args.kwargs["conversation_id"] == THREAD, "for its thread"


async def test_a_switch_without_a_pick_changes_nothing(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Switch whose pick isn't text is refused without a write."""
    answer = await _on(_press("confirm", v=3), monkeypatch, actor=_member())
    assert module.CANNOT_VERIFY in str(answer.value), "can't verify it"
    flow.confirm.assert_not_awaited()


async def test_cancel_on_an_old_card_changes_nothing(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel on an old card closes it in one line."""
    answer = await _on(_press("cancel"), monkeypatch, actor=_member())
    assert "Nothing changed." in _text(answer.value), "one line"
    flow.pick.assert_not_awaited()
    flow.confirm.assert_not_awaited()


@pytest.mark.parametrize("actor", [None, dataclasses.replace(_member(), foreign=True)])
async def test_an_unverified_or_foreign_clicker_is_refused(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, actor: Actor | None
) -> None:
    """A press with no verified member of this tenant behind it changes nothing."""
    answer = await _on(_press("pick:default"), monkeypatch, actor=actor)
    assert module.CANNOT_VERIFY in str(answer.value), "can't verify them"
    flow.pick.assert_not_awaited()


async def test_a_press_rechecks_the_right_to_choose(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every press checks the right to choose again."""
    flow.may.return_value = False
    answer = await _on(_press("pick:openai:gpt-6"), monkeypatch, actor=_member())
    assert module.NOT_ALLOWED in _text(answer.value), "names who can help"
    flow.pick.assert_not_awaited()


@pytest.mark.parametrize(
    ("conversation", "kind", "found"),
    [
        (THREAD, "channel", (CHANNEL, THREAD)),
        (CHANNEL, "channel", None),
        ("19:chat@unq.gbl.spaces", "groupChat", ("19:chat@unq.gbl.spaces",) * 2),
        ("a:1:chat", "personal", ("a:1:chat",) * 2),
    ],
)
def test_the_conversation_comes_from_where_the_card_was_pressed(
    conversation: str, kind: str, found: tuple[str, str] | None
) -> None:
    """The thread and its channel come from the pressed card's own conversation."""
    assert module.where(conversation, kind) == found, "read from the conversation"
