"""Slack's model choice for one thread: the shortcut, the pick, one Confirm, and the ask-again card."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import MISSING, fields, replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.slack import thread_model as module
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.thread_model_flow import ThreadConfirm, ThreadOption, ThreadPickResult
from daimon.core.thread_model_reask import ThreadReask
from daimon.testing.turn_fakes import deps_for
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

THREAD = "1700000000.000100"
CONFIRM = ThreadConfirm("openai:gpt-6", "digest-1", 3)
LINES = ("Model: GPT-6", "Unsupported skills: None", "Cost: $2 per 1M tokens")


def _runtime() -> SlackRuntime:
    needed = [
        f.name
        for f in fields(SlackRuntime)
        if f.default is MISSING and f.default_factory is MISSING
    ]
    return SlackRuntime(**{name: MagicMock() for name in needed})  # type: ignore[arg-type]  # fmt: skip


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Slack's client, the membership checks and the shared flow, each replaced."""
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(module, "resolve_is_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "may_choose_thread_model", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "build_thread_pick", AsyncMock(return_value=MagicMock()))
    options = (ThreadOption("GPT-6 ($2)", "openai:gpt-6"), ThreadOption("Agent default", "default"))
    monkeypatch.setattr(module, "thread_options", AsyncMock(return_value=options))
    sent = AsyncMock()
    monkeypatch.setattr(
        module, "AsyncWebhookClient", MagicMock(return_value=MagicMock(send_dict=sent))
    )
    client.replaced = sent
    return client


def _shortcut(message: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "message_action",
        "callback_id": module.SHORTCUT_CALLBACK_ID,
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": message or {"ts": THREAD},
    }


def _pick(choice: str, *, block_id: str = f"thread_model:{THREAD}") -> dict[str, Any]:
    return {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "response_url": "https://hooks.example/r",
        "actions": [
            {
                "action_id": module.PICK_ACTION_ID,
                "block_id": block_id,
                "selected_option": {"value": choice},
            }
        ],
    }


def _click(action_id: str, value: str) -> dict[str, Any]:
    return {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "response_url": "https://hooks.example/r",
        "actions": [{"action_id": action_id, "value": value}],
    }


def _replaced(seams: MagicMock) -> dict[str, Any]:
    assert seams.replaced.await_args is not None
    return seams.replaced.await_args.args[0]


def _buttons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


async def test_the_shortcut_shows_the_select_in_the_thread_to_its_caller(seams: MagicMock) -> None:
    await module.handle_thread_model_shortcut(
        _runtime(), _shortcut({"ts": "9", "thread_ts": THREAD})
    )

    posted = seams.chat_postEphemeral.await_args.kwargs
    assert (posted["channel"], posted["user"], posted["thread_ts"]) == ("C1", "U1", THREAD)
    (select,) = _buttons(posted["blocks"])
    assert select["type"] == "static_select"
    assert [o["value"] for o in select["options"]] == ["openai:gpt-6", "default"]
    assert posted["blocks"][1]["block_id"] == f"thread_model:{THREAD}", "the thread rides the block"


async def test_the_shortcut_on_a_thread_root_uses_its_own_ts(seams: MagicMock) -> None:
    await module.handle_thread_model_shortcut(_runtime(), _shortcut({"ts": THREAD}))

    assert seams.chat_postEphemeral.await_args.kwargs["thread_ts"] == THREAD


async def test_someone_who_may_not_start_a_turn_is_refused_naming_who_can_help(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    monkeypatch.setattr(module, "may_choose_thread_model", AsyncMock(return_value=False))

    await module.handle_thread_model_shortcut(_runtime(), _shortcut())

    posted = seams.chat_postEphemeral.await_args.kwargs
    assert "blocks" not in posted, "no select is offered"
    assert "admin" in posted["attachments"][0]["fallback"]
    module.build_thread_pick.assert_not_awaited()  # type: ignore[attr-defined]


async def test_a_cross_provider_pick_shows_what_changes_with_one_confirm(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    monkeypatch.setattr(
        module, "pick_thread_model", AsyncMock(return_value=ThreadPickResult("", CONFIRM, LINES))
    )

    await module.handle_thread_model_pick(_runtime(), _pick("openai:gpt-6"))

    fields_ = _replaced(seams)
    assert fields_["replace_original"] is True
    text = fields_["blocks"][0]["text"]["text"]
    assert all(line in text for line in LINES), "the facts are shown"
    confirm, cancel = _buttons(fields_["blocks"])
    assert (confirm["action_id"], cancel["action_id"]) == (
        module.CONFIRM_ACTION_ID,
        module.CANCEL_ACTION_ID,
    )
    assert json.loads(confirm["value"]) == {
        "v": "openai:gpt-6",
        "d": "digest-1",
        "r": 3,
        "c": THREAD,
    }
    kept = module.pick_thread_model.await_args.kwargs  # type: ignore[attr-defined]
    assert (kept["conversation_id"], kept["choice"], kept["platform"]) == (THREAD, "openai:gpt-6", "slack")  # fmt: skip


async def test_a_refused_pick_is_one_line_with_the_core_words(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    refusal = "Ask an admin or an owner to allow it. Nothing changed."
    monkeypatch.setattr(
        module, "pick_thread_model", AsyncMock(return_value=ThreadPickResult(refusal))
    )

    await module.handle_thread_model_pick(_runtime(), _pick("openai:gpt-6"))

    (card,) = _replaced(seams)["attachments"]
    assert card["fallback"] == refusal, "the core text, unchanged"
    assert "blocks" not in _replaced(seams) or _replaced(seams)["blocks"] == []


async def test_a_pick_that_cannot_be_confirmed_ends_with_nothing_changed(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    monkeypatch.setattr(
        module, "pick_thread_model", AsyncMock(return_value=ThreadPickResult("", None, LINES))
    )

    await module.handle_thread_model_pick(_runtime(), _pick("openai:gpt-6"))

    (card,) = _replaced(seams)["attachments"]
    assert card["fallback"].endswith("Nothing changed.")


async def test_a_pick_without_a_thread_in_its_block_does_nothing(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    keep = AsyncMock()
    monkeypatch.setattr(module, "pick_thread_model", keep)

    await module.handle_thread_model_pick(_runtime(), _pick("openai:gpt-6", block_id="other"))

    keep.assert_not_awaited()
    seams.replaced.assert_not_awaited()


async def test_confirm_keeps_the_pick_at_the_digest_it_was_shown(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    kept = AsyncMock(return_value="This conversation runs on GPT-6.")
    monkeypatch.setattr(module, "confirm_thread_pick", kept)
    value = module._confirm_value(CONFIRM, THREAD)  # pyright: ignore[reportPrivateUsage]

    await module.handle_thread_model_confirm(_runtime(), _click(module.CONFIRM_ACTION_ID, value))

    assert kept.await_args.args[1] == CONFIRM
    assert kept.await_args.kwargs == {"conversation_id": THREAD, "admin": True}
    (card,) = _replaced(seams)["attachments"]
    assert card["fallback"] == "This conversation runs on GPT-6."


async def test_confirm_from_someone_who_may_not_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    kept = AsyncMock()
    monkeypatch.setattr(module, "confirm_thread_pick", kept)
    monkeypatch.setattr(module, "may_choose_thread_model", AsyncMock(return_value=False))
    value = module._confirm_value(CONFIRM, THREAD)  # pyright: ignore[reportPrivateUsage]

    await module.handle_thread_model_confirm(_runtime(), _click(module.CONFIRM_ACTION_ID, value))

    kept.assert_not_awaited()
    (card,) = _replaced(seams)["attachments"]
    assert "admin" in card["fallback"] and card["fallback"].endswith("Nothing changed.")


async def test_a_forged_confirm_value_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    kept = AsyncMock()
    monkeypatch.setattr(module, "confirm_thread_pick", kept)

    await module.handle_thread_model_confirm(_runtime(), _click(module.CONFIRM_ACTION_ID, "{x"))

    kept.assert_not_awaited()
    seams.replaced.assert_not_awaited()


async def test_cancel_leaves_one_line(seams: MagicMock) -> None:
    await module.handle_thread_model_cancel(_runtime(), _click(module.CANCEL_ACTION_ID, "cancel"))

    (card,) = _replaced(seams)["attachments"]
    assert card["fallback"] == "Nothing changed."


def _ask(confirm: ThreadConfirm | None = CONFIRM) -> ThreadReask:
    return ThreadReask(
        tenant_id=MagicMock(),
        workspace_id="T1",
        platform="slack",
        channel_id="C1",
        thread_id=THREAD,
        conversation_id=THREAD,
        agent_name="Helper",
        lines=LINES,
        confirm=confirm,
    )


async def test_the_ask_again_card_is_posted_in_the_thread_with_one_confirm(
    seams: MagicMock,
) -> None:
    await module.slack_thread_reask_notifier(_runtime())(_ask())

    posted = seams.chat_postMessage.await_args.kwargs
    assert (posted["channel"], posted["thread_ts"]) == ("C1", THREAD)
    (attachment,) = posted["attachments"]
    confirm, cancel = _buttons(attachment["blocks"])
    assert confirm["action_id"] == module.CONFIRM_ACTION_ID
    assert json.loads(confirm["value"])["c"] == THREAD
    assert cancel["action_id"] == module.CANCEL_ACTION_ID


async def test_a_refused_preview_is_posted_without_buttons(seams: MagicMock) -> None:
    await module.slack_thread_reask_notifier(_runtime())(_ask(None))

    (attachment,) = seams.chat_postMessage.await_args.kwargs["attachments"]
    assert _buttons(attachment["blocks"]) == []


async def test_a_workspace_without_a_client_raises_so_the_next_message_asks_again(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=None))

    with pytest.raises(RuntimeError):
        await module.slack_thread_reask_notifier(_runtime())(_ask())


def test_the_slack_app_wires_the_ask_again_card_into_its_turns() -> None:
    runtime = replace(_runtime(), turn_deps=deps_for())

    wired = module.with_thread_reask_notifier(runtime)

    assert wired.turn_deps.thread_reask_notifier is not None
    stand_in = MagicMock()
    assert module.with_thread_reask_notifier(stand_in) is stand_in, "a test runtime is untouched"


@dataclasses.dataclass
class _Socket:
    sent: list[SocketModeResponse] = dataclasses.field(default_factory=list[SocketModeResponse])

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.sent.append(response)


def _app() -> SlackApp:
    settings = MagicMock()
    settings.slack.max_concurrent_turns_per_tenant = 3
    return SlackApp(runtime=replace(_runtime(), settings=settings, turn_deps=deps_for()))


async def _drain(app: SlackApp) -> None:
    pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize(
    ("payload", "handler"),
    [
        (_shortcut() | {"user": {"id": "U1", "team_id": "T1"}}, "handle_thread_model_shortcut"),
        (
            _pick("openai:gpt-6") | {"type": "block_actions", "user": {"id": "U1", "team_id": "T1"}},
            "handle_thread_model_pick",
        ),
        (
            _click(module.CONFIRM_ACTION_ID, "x") | {"type": "block_actions", "user": {"id": "U1", "team_id": "T1"}},
            "handle_thread_model_confirm",
        ),
        (
            _click(module.CANCEL_ACTION_ID, "x") | {"type": "block_actions", "user": {"id": "U1", "team_id": "T1"}},
            "handle_thread_model_cancel",
        ),
    ],
)  # fmt: skip
async def test_the_app_routes_the_shortcut_and_its_controls(
    payload: dict[str, Any], handler: str
) -> None:
    app, socket, calls = _app(), _Socket(), list[str]()

    async def _fake(runtime: Any, body: dict[str, Any]) -> None:
        calls.append(handler)

    with patch(f"daimon.adapters.slack.app.{handler}", new=_fake):
        await app.on_request(socket, SocketModeRequest("interactive", "env", payload))  # type: ignore[arg-type]
        await _drain(app)

    assert socket.sent, "acked"
    assert calls == [handler]


async def test_an_external_member_cannot_open_the_shortcut() -> None:
    app, socket, calls = _app(), _Socket(), list[str]()
    payload = _shortcut() | {"user": {"id": "U9", "team_id": "TOTHER"}}

    async def _fake(runtime: Any, body: dict[str, Any]) -> None:
        calls.append("opened")

    with (
        patch("daimon.adapters.slack.app.handle_thread_model_shortcut", new=_fake),
        patch.object(SlackApp, "_refuse_external_click", new=AsyncMock()) as refused,
    ):
        await app.on_request(socket, SocketModeRequest("interactive", "env", payload))  # type: ignore[arg-type]
        await _drain(app)

    assert calls == []
    refused.assert_awaited_once()
