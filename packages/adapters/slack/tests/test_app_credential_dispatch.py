"""Dispatch tests for the credential-request routes in SlackApp.on_request.

Covers:
- credential_request block_action: empty ack first, then the click handler
  is spawned with the payload.
- credential_request__env view_submission: ack carries the field-error
  payload for an empty value (no background run spawns), and an empty ack
  plus a spawned runner for a valid value.
- An external Slack Connect click never reaches the handler.
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
from daimon.adapters.slack.runtime import SlackRuntime
from pydantic import SecretStr
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse


@dataclasses.dataclass
class _FakeSocketClient:
    call_log: list[str] = dataclasses.field(default_factory=list[str])
    sent_responses: list[SocketModeResponse] = dataclasses.field(
        default_factory=list[SocketModeResponse]
    )

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.call_log.append("send_socket_mode_response")
        self.sent_responses.append(response)


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


def _click_payload(*, external: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "block_actions",
        "team": {"id": "T_TEST"},
        "user": {"id": "U_TEST"},
        "channel": {"id": "C_TEST"},
        "container": {"message_ts": "1700000003.000300"},
        "trigger_id": "trig_cred_click",
        "actions": [{"action_id": "credential_request", "value": "tok_click"}],
    }
    if external:
        payload["user"]["team_id"] = "T_OTHER"
        payload["is_enterprise_install"] = False
    return payload


def _submission_payload(value: str) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": "T_TEST"},
        "user": {"id": "U_TEST"},
        "view": {
            "callback_id": "credential_request__env",
            "private_metadata": json.dumps(
                {
                    "token": "tok_submit",
                    "channel_id": "C_TEST",
                    "message_ts": "1700000003.000300",
                },
                separators=(",", ":"),
            ),
            "state": {
                "values": {
                    "credential__value": {
                        "credential__value": {"type": "plain_text_input", "value": value}
                    }
                }
            },
        },
    }


async def test_credential_click_acks_first_then_spawns_handler() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    seen: list[dict[str, Any]] = []

    async def _fake_click(runtime: Any, payload: Any) -> None:
        seen.append(payload)

    req = SocketModeRequest(
        type="interactive", envelope_id="env_cred_click_001", payload=_click_payload()
    )
    with patch("daimon.adapters.slack.app.handle_credential_request_click", new=_fake_click):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response"
    assert len(seen) == 1 and seen[0]["actions"][0]["value"] == "tok_click"


async def test_env_submission_with_empty_value_acks_errors_and_spawns_nothing() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    ran: list[str] = []

    async def _fake_run(*args: Any, **kwargs: Any) -> None:
        ran.append("ran")

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_cred_submit_empty",
        payload=_submission_payload("   "),
    )
    with patch("daimon.adapters.slack.app.run_env_credential_submission", new=_fake_run):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    ack = fake_client.sent_responses[0].payload
    assert isinstance(ack, dict) and ack.get("response_action") == "errors", (
        "an empty value must ack with the field-error payload"
    )
    assert not ran, "a rejected submission must not spawn a background run"


async def test_env_submission_with_value_acks_empty_and_spawns_runner() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    ran: list[dict[str, Any]] = []

    async def _fake_run(runtime: Any, **kwargs: Any) -> None:
        ran.append(kwargs)

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_cred_submit_ok",
        payload=_submission_payload("s3cr3t"),
    )
    with patch("daimon.adapters.slack.app.run_env_credential_submission", new=_fake_run):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    ack = fake_client.sent_responses[0].payload
    assert not ack, "a valid submission must ack empty (close the modal)"
    assert len(ran) == 1
    assert ran[0]["token"] == "tok_submit"
    assert ran[0]["value"] == "s3cr3t"
    assert ran[0]["message_ts"] == "1700000003.000300"


async def test_external_connect_click_never_reaches_the_handler() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    seen: list[dict[str, Any]] = []

    async def _fake_click(runtime: Any, payload: Any) -> None:
        seen.append(payload)

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_cred_click_external",
        payload=_click_payload(external=True),
    )
    with patch("daimon.adapters.slack.app.handle_credential_request_click", new=_fake_click):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert not seen, "an external Slack Connect click must be rejected before dispatch"
