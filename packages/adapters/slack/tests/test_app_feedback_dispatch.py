"""Dispatch tests for the feedback routes in SlackApp.on_request.

Covers:
- feedback_vote:* block_action: empty ack first, then handle_feedback_vote spawns
- feedback_text view_submission: valid text acks empty and spawns
  run_feedback_text_submission; whitespace-only text acks with a
  response_action errors payload and spawns nothing
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from anthropic import AsyncAnthropic
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.feedback import (
    FEEDBACK_DETAILS_ACTION_ID,
    FEEDBACK_TEXT_CALLBACK_ID,
    FEEDBACK_VOTE_UP,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.support_escalation import ASK_HUMAN_ACTION_ID, SUPPORT_CALLBACK_ID
from pydantic import SecretStr
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse


@dataclasses.dataclass
class _FakeSocketClient:
    """Minimal Socket Mode client fake — records call order and ack payloads."""

    call_log: list[str] = dataclasses.field(default_factory=list[str])
    sent_responses: list[SocketModeResponse] = dataclasses.field(
        default_factory=list[SocketModeResponse]
    )

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.call_log.append("send_socket_mode_response")
        self.sent_responses.append(response)

    async def close(self) -> None:
        self.call_log.append("close")


def _make_app() -> SlackApp:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr("dummykey"),)
    settings.slack.max_concurrent_turns_per_tenant = 3
    runtime = SlackRuntime(
        settings=settings,
        anthropic=MagicMock(spec=AsyncAnthropic),
        sessionmaker=MagicMock(),
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )
    return SlackApp(runtime=runtime)


async def _drain(app: SlackApp) -> None:
    pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _vote_request() -> SocketModeRequest:
    return SocketModeRequest(
        type="interactive",
        envelope_id="env_feedback_vote_001",
        payload={
            "type": "block_actions",
            "team": {"id": "T_TEST"},
            "user": {"id": "U_TEST", "team_id": "T_TEST"},
            "channel": {"id": "C_TEST"},
            "container": {"message_ts": "1700000001.000100"},
            "trigger_id": "trig_feedback_001",
            "actions": [{"action_id": FEEDBACK_VOTE_UP}],
        },
    )


def _text_submission_request(text: str) -> SocketModeRequest:
    return SocketModeRequest(
        type="interactive",
        envelope_id="env_feedback_text_001",
        payload={
            "type": "view_submission",
            "team": {"id": "T_TEST"},
            "user": {"id": "U_TEST"},
            "view": {
                "callback_id": FEEDBACK_TEXT_CALLBACK_ID,
                "id": "V_TEST",
                "private_metadata": json.dumps(
                    {"feedback_id": "0b6ef01e-9f0a-4bb6-a30c-111111111111", "channel_id": "C_TEST"}
                ),
                "state": {
                    "values": {
                        "feedback_text_block": {"feedback_text_input": {"value": text}},
                    }
                },
            },
        },
    )


async def test_feedback_vote_block_action_acks_then_spawns_handler() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    spawned: list[str] = []

    async def _fake_handle(runtime: Any, payload: Any) -> None:
        spawned.append(str(payload["actions"][0]["action_id"]))

    with patch("daimon.adapters.slack.app.handle_feedback_vote", new=_fake_handle):
        await app.on_request(fake_client, _vote_request())  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "a feedback vote must ack first, before any handler work"
    )
    assert spawned == [FEEDBACK_VOTE_UP], "handle_feedback_vote must be spawned for the vote"


async def test_feedback_details_button_acks_then_spawns_its_handler() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    spawned: list[str] = []
    request = _vote_request()
    request.payload["actions"] = [{"action_id": FEEDBACK_DETAILS_ACTION_ID, "value": "{}"}]

    async def _fake_details(runtime: Any, payload: Any) -> None:
        spawned.append(str(payload["actions"][0]["action_id"]))

    async def _fake_vote(runtime: Any, payload: Any) -> None:
        spawned.append("vote")

    with (
        patch("daimon.adapters.slack.app.handle_feedback_details_click", new=_fake_details),
        patch("daimon.adapters.slack.app.handle_feedback_vote", new=_fake_vote),
    ):
        await app.on_request(fake_client, request)  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response"
    assert spawned == [FEEDBACK_DETAILS_ACTION_ID], "the details button is not a vote"


async def test_feedback_text_submission_valid_acks_empty_and_spawns_run() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    ran: list[str] = []

    async def _fake_run(runtime: Any, **kwargs: Any) -> None:
        ran.append(str(kwargs["decision"].text))

    with patch("daimon.adapters.slack.app.run_feedback_text_submission", new=_fake_run):
        await app.on_request(fake_client, _text_submission_request("it was wrong"))  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.sent_responses[0].payload is None, (
        "valid feedback text must ack empty so the modal closes"
    )
    assert ran == ["it was wrong"], "run_feedback_text_submission must receive the text"


async def test_feedback_text_submission_whitespace_acks_errors_and_spawns_nothing() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    ran: list[str] = []

    async def _fake_run(runtime: Any, **kwargs: Any) -> None:
        ran.append(str(kwargs["decision"].text))

    with patch("daimon.adapters.slack.app.run_feedback_text_submission", new=_fake_run):
        await app.on_request(fake_client, _text_submission_request("   "))  # type: ignore[arg-type]
        await _drain(app)

    ack = fake_client.sent_responses[0].payload
    assert isinstance(ack, dict) and ack.get("response_action") == "errors", (
        "whitespace-only text must ack with a field error"
    )
    assert ran == [], "a rejected submission must not spawn the background write"


def _ask_human_request() -> SocketModeRequest:
    return SocketModeRequest(
        type="interactive",
        envelope_id="env_support_click_001",
        payload={
            "type": "block_actions",
            "team": {"id": "T_TEST"},
            "user": {"id": "U_TEST", "team_id": "T_TEST"},
            "channel": {"id": "C_TEST"},
            "container": {"message_ts": "1700000001.000100"},
            "trigger_id": "trig_support_001",
            "actions": [{"action_id": ASK_HUMAN_ACTION_ID}],
        },
    )


def _support_submission_request(note: str, *, user_team: str = "T_TEST") -> SocketModeRequest:
    return SocketModeRequest(
        type="interactive",
        envelope_id="env_support_submit_001",
        payload={
            "type": "view_submission",
            "team": {"id": "T_TEST"},
            "user": {"id": "U_TEST", "team_id": user_team},
            "view": {
                "callback_id": SUPPORT_CALLBACK_ID,
                "id": "V_TEST",
                "private_metadata": json.dumps(
                    {"channel_id": "C_TEST", "message_ts": "1700000001.000100"}
                ),
                "state": {
                    "values": {"support_note_block": {"support_note_input": {"value": note}}}
                },
            },
        },
    )


async def test_ask_human_click_acks_then_spawns_handler() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    spawned: list[str] = []

    async def _fake_handle(runtime: Any, payload: Any) -> None:
        spawned.append(str(payload["actions"][0]["action_id"]))

    with patch("daimon.adapters.slack.app.handle_ask_human_click", new=_fake_handle):
        await app.on_request(fake_client, _ask_human_request())  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response"
    assert spawned == [ASK_HUMAN_ACTION_ID]


async def test_support_submission_acks_empty_and_spawns_run() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    ran: list[str] = []

    async def _fake_run(runtime: Any, submission: Any) -> None:
        ran.append(str(submission.note))

    with patch("daimon.adapters.slack.app.run_support_submission", new=_fake_run):
        await app.on_request(fake_client, _support_submission_request("help"))  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.sent_responses[0].payload is None
    assert ran == ["help"]


async def test_external_support_submission_closes_and_spawns_nothing() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    ran: list[str] = []

    async def _fake_run(runtime: Any, submission: Any) -> None:
        ran.append(str(submission.note))

    with patch("daimon.adapters.slack.app.run_support_submission", new=_fake_run):
        await app.on_request(
            fake_client,
            _support_submission_request("help", user_team="T_ELSEWHERE"),  # type: ignore[arg-type]
        )
        await _drain(app)

    assert fake_client.sent_responses[0].payload is None
    assert ran == [], "an external Slack Connect member's note must go nowhere"
