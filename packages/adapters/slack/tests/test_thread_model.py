"""Slack's model choice for one thread: the shortcut, then a pick that applies at once."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import MISSING, fields, replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.slack import thread_model as module
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.thread_model_flow import ThreadOption
from daimon.testing.shared_helpers import drain as _drain
from daimon.testing.turn_fakes import deps_for
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

THREAD = "1700000000.000100"
KEPT = "This conversation runs on GPT-6. Off there: memory."
#: A Switch posted before picks applied at once: its digest and revision are ignored.
OLD_CONFIRM = json.dumps({"v": "openai:gpt-6", "d": "digest-1", "r": 3, "c": THREAD})


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
    assert seams.replaced.await_args is not None, "the card was replaced"
    return seams.replaced.await_args.args[0]


def _buttons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


async def test_the_shortcut_shows_the_select_in_the_thread_to_its_caller(seams: MagicMock) -> None:
    """The select goes to its caller only, in the message's thread, carrying the thread."""
    await module.handle_thread_model_shortcut(
        _runtime(), _shortcut({"ts": "9", "thread_ts": THREAD})
    )

    posted = seams.chat_postEphemeral.await_args.kwargs
    assert (posted["channel"], posted["user"], posted["thread_ts"]) == ("C1", "U1", THREAD), (
        "in the thread"
    )
    (select,) = _buttons(posted["blocks"])
    assert select["type"] == "static_select", "one select"
    assert [o["value"] for o in select["options"]] == ["openai:gpt-6", "default"], "the options"
    assert posted["blocks"][1]["block_id"] == f"thread_model:{THREAD}", "the thread rides the block"


async def test_the_shortcut_on_a_thread_root_uses_its_own_ts(seams: MagicMock) -> None:
    """A thread's root message is its own thread."""
    await module.handle_thread_model_shortcut(_runtime(), _shortcut({"ts": THREAD}))

    assert seams.chat_postEphemeral.await_args.kwargs["thread_ts"] == THREAD, "the root's ts"


async def test_someone_who_may_not_start_a_turn_is_refused_naming_who_can_help(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    """No select for someone who may not choose; the refusal names who can help."""
    monkeypatch.setattr(module, "may_choose_thread_model", AsyncMock(return_value=False))

    await module.handle_thread_model_shortcut(_runtime(), _shortcut())

    posted = seams.chat_postEphemeral.await_args.kwargs
    assert "blocks" not in posted, "no select is offered"
    assert "admin" in posted["attachments"][0]["fallback"], "names who can help"
    module.build_thread_pick.assert_not_awaited()  # type: ignore[attr-defined]


async def test_a_cross_provider_pick_is_kept_at_once_with_no_buttons(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    """A pick on another provider is kept in one step: a neutral notice, never a Confirm."""
    monkeypatch.setattr(module, "pick_thread_model", AsyncMock(return_value=KEPT))

    await module.handle_thread_model_pick(_runtime(), _pick("openai:gpt-6"))

    fields_ = _replaced(seams)
    assert fields_["replace_original"] is True, "the select is replaced"
    assert _buttons(fields_["blocks"]) == [], "nothing to confirm"
    (card,) = fields_["attachments"]
    assert card["fallback"] == KEPT, "the core note, unchanged"
    kept = module.pick_thread_model.await_args.kwargs  # type: ignore[attr-defined]
    assert (kept["conversation_id"], kept["choice"], kept["platform"]) == (THREAD, "openai:gpt-6", "slack"), "kept for this thread"  # fmt: skip


async def test_a_pick_without_a_thread_in_its_block_does_nothing(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    """A select that doesn't carry its thread changes nothing."""
    keep = AsyncMock()
    monkeypatch.setattr(module, "pick_thread_model", keep)

    await module.handle_thread_model_pick(_runtime(), _pick("openai:gpt-6", block_id="other"))

    keep.assert_not_awaited()
    seams.replaced.assert_not_awaited()


async def test_switch_on_an_old_card_keeps_its_pick_and_ignores_its_digest(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    """A Switch posted before picks applied at once keeps its pick as a new pick would."""
    kept = AsyncMock(return_value=KEPT)
    monkeypatch.setattr(module, "confirm_thread_pick", kept)

    await module.handle_thread_model_confirm(
        _runtime(), _click(module.CONFIRM_ACTION_ID, OLD_CONFIRM)
    )

    assert kept.await_args.args[1] == "openai:gpt-6", "only the pick is read"
    assert kept.await_args.kwargs == {"conversation_id": THREAD, "admin": True}, "for its thread"
    (card,) = _replaced(seams)["attachments"]
    assert card["fallback"] == KEPT, "the note replaces the card"


async def test_switch_from_someone_who_may_not_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    """An old card's Switch is checked again like a fresh pick."""
    kept = AsyncMock()
    monkeypatch.setattr(module, "confirm_thread_pick", kept)
    monkeypatch.setattr(module, "may_choose_thread_model", AsyncMock(return_value=False))

    await module.handle_thread_model_confirm(
        _runtime(), _click(module.CONFIRM_ACTION_ID, OLD_CONFIRM)
    )

    kept.assert_not_awaited()
    (card,) = _replaced(seams)["attachments"]
    assert "admin" in card["fallback"], "names who can help"
    assert card["fallback"].endswith("Nothing changed."), "and that nothing changed"


async def test_a_forged_switch_value_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, seams: MagicMock
) -> None:
    """A value that doesn't parse is dropped without a reply."""
    kept = AsyncMock()
    monkeypatch.setattr(module, "confirm_thread_pick", kept)

    await module.handle_thread_model_confirm(_runtime(), _click(module.CONFIRM_ACTION_ID, "{x"))

    kept.assert_not_awaited()
    seams.replaced.assert_not_awaited()


async def test_cancel_on_an_old_card_leaves_one_line(seams: MagicMock) -> None:
    """Cancel on an old card closes it in one line."""
    await module.handle_thread_model_cancel(_runtime(), _click(module.CANCEL_ACTION_ID, "cancel"))

    (card,) = _replaced(seams)["attachments"]
    assert card["fallback"] == "Nothing changed.", "one line"


@dataclasses.dataclass
class _Socket:
    sent: list[SocketModeResponse] = dataclasses.field(default_factory=list[SocketModeResponse])

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.sent.append(response)


def _app() -> SlackApp:
    settings = MagicMock()
    settings.slack.max_concurrent_turns_per_tenant = 3
    return SlackApp(runtime=replace(_runtime(), settings=settings, turn_deps=deps_for()))


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
