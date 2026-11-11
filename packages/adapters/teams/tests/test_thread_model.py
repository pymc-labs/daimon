"""Teams' `model` command: the choices, one Confirm, and the ask-again card."""

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
from daimon.core.thread_model_flow import ThreadConfirm, ThreadOption, ThreadPickResult
from daimon.core.thread_model_reask import ThreadReask

CHANNEL = "19:abc@thread.tacv2"
THREAD = f"{CHANNEL};messageid=1700"
TENANT = uuid.uuid4()
CONFIRM = ThreadConfirm("openai:gpt-6", "digest-1", 3)
LINES = ("Model: GPT-6", "Cost: $2 per 1M tokens")
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
        pick=AsyncMock(return_value=ThreadPickResult("", CONFIRM, LINES)),
        confirm=AsyncMock(return_value="Switched to GPT-6."),
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
    context, sent = _context()
    await module.show_model(context)
    assert [(b["verb"], b["data"]["op"]) for b in _buttons(sent[0])] == [
        ("thread_model", "pick:openai:gpt-6"),
        ("thread_model", "pick:default"),
    ]
    assert flow.may.await_args.kwargs["thread_id"] == THREAD


async def test_a_sender_without_the_right_gets_one_line_naming_who_can_help(
    flow: SimpleNamespace,
) -> None:
    flow.may.return_value = False
    context, sent = _context()
    await module.show_model(context)
    assert module.NOT_ALLOWED in _text(sent[0]) and not _buttons(sent[0])


async def test_a_command_moved_to_the_chat_points_back_to_the_conversation(
    flow: SimpleNamespace,
) -> None:
    context, sent = _context(asked_in=THREAD)
    await module.show_model(context)
    assert module.IN_THE_CONVERSATION in _text(sent[0])
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


async def test_a_cross_provider_pick_shows_the_preview_and_one_confirm(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = await _on(_press("pick:openai:gpt-6"), monkeypatch, actor=_member())
    assert [b["data"]["op"] for b in _buttons(answer.value)] == ["confirm", "cancel"]
    data = _buttons(answer.value)[0]["data"]
    assert (data["v"], data["d"], data["r"]) == ("openai:gpt-6", "digest-1", 3)
    assert all(line in _text(answer.value) for line in LINES)
    assert flow.pick.await_args.kwargs["conversation_id"] == THREAD
    assert flow.pick.await_args.kwargs["choice"] == "openai:gpt-6"


async def test_a_refused_pick_is_one_line_with_the_core_text(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow.pick.return_value = ThreadPickResult("Ask an admin to allow GPT-6. Nothing changed.")
    answer = await _on(_press("pick:openai:gpt-6"), monkeypatch, actor=_member())
    assert "Ask an admin to allow GPT-6. Nothing changed." in _text(answer.value)
    assert not _buttons(answer.value)


async def test_confirm_keeps_the_pick_the_card_was_shown_at(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _press("confirm", v=CONFIRM.value, d=CONFIRM.digest, r=CONFIRM.revision)
    answer = await _on(ctx, monkeypatch, actor=_member())
    assert "Switched to GPT-6." in _text(answer.value)
    assert flow.confirm.await_args.args[1] == CONFIRM
    assert flow.confirm.await_args.kwargs["conversation_id"] == THREAD


async def test_a_forged_confirm_changes_nothing(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = await _on(
        _press("confirm", v="x", d="y", r="not a number"), monkeypatch, actor=_member()
    )
    assert module.CANNOT_VERIFY in str(answer.value)
    flow.confirm.assert_not_awaited()


async def test_cancel_changes_nothing(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = await _on(_press("cancel"), monkeypatch, actor=_member())
    assert "Nothing changed." in _text(answer.value)
    flow.pick.assert_not_awaited()
    flow.confirm.assert_not_awaited()


@pytest.mark.parametrize("actor", [None, dataclasses.replace(_member(), foreign=True)])
async def test_an_unverified_or_foreign_clicker_is_refused(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, actor: Actor | None
) -> None:
    answer = await _on(_press("pick:default"), monkeypatch, actor=actor)
    assert module.CANNOT_VERIFY in str(answer.value)
    flow.pick.assert_not_awaited()


async def test_a_press_rechecks_the_right_to_choose(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow.may.return_value = False
    answer = await _on(_press("pick:openai:gpt-6"), monkeypatch, actor=_member())
    assert module.NOT_ALLOWED in _text(answer.value)
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
    assert module.where(conversation, kind) == found


def _ask(confirm: ThreadConfirm | None = CONFIRM) -> ThreadReask:
    return ThreadReask(
        tenant_id=TENANT, workspace_id="w", platform="teams", channel_id=CHANNEL,
        thread_id=THREAD, conversation_id=THREAD, agent_name="Helper", lines=LINES,
        confirm=confirm,
    )  # fmt: skip


async def test_the_ask_again_card_is_posted_in_the_conversation() -> None:
    direct = MagicMock(post_card=AsyncMock(return_value="m1"))
    await module.teams_thread_reask_notifier(direct)(_ask())
    conversation, activity = direct.post_card.await_args.args
    assert conversation == THREAD
    card = activity.attachments[0].content
    assert [b["data"]["op"] for b in _buttons(card)] == ["confirm", "cancel"]


async def test_the_ask_again_card_without_a_confirm_has_no_buttons() -> None:
    direct = MagicMock(post_card=AsyncMock(return_value="m1"))
    await module.teams_thread_reask_notifier(direct)(_ask(None))
    assert not _buttons(direct.post_card.await_args.args[1].attachments[0].content)


async def test_a_failed_post_raises_so_a_later_message_asks_again() -> None:
    direct = MagicMock(post_card=AsyncMock(side_effect=module.TEAMS_SEND_ERRORS[0]("down")))
    with pytest.raises(module.TEAMS_SEND_ERRORS):
        await module.teams_thread_reask_notifier(direct)(_ask())


def test_without_a_way_to_post_the_runtime_is_unchanged() -> None:
    runtime = MagicMock()
    assert module.with_thread_reask_notifier(runtime, None) is runtime
