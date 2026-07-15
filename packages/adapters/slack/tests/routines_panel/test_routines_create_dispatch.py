"""Dispatch tests for routines__create view_submission + routines_create block_action.

Covers:
- routines__create view_submission with valid fields acks response_action=clear
  and spawns the background run; channel_id is sourced from private_metadata.
- routines_create block_action acks empty first, then spawns handle_routine_action.
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
        http_client=MagicMock(spec=httpx.AsyncClient),
    )
    return SlackApp(runtime=runtime)


async def _drain(app: SlackApp) -> None:
    pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _create_view_submission_payload(*, cron: str = "0 18 * * *") -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": "T_TEST"},
        "user": {"id": "U_TEST"},
        "view": {
            "callback_id": "routines__create",
            "id": "V_TEST",
            "private_metadata": json.dumps({"team_id": "T_TEST", "channel_id": "C_FROM_META"}),
            "state": {
                "values": {
                    "routines_create__agent": {
                        "routines_create__agent": {
                            "type": "static_select",
                            "selected_option": {"value": "daimon"},
                        }
                    },
                    "routines_create__cron": {
                        "routines_create__cron": {"value": cron},
                    },
                    "routines_create__timezone": {
                        "routines_create__timezone": {"value": "UTC"},
                    },
                    "routines_create__message": {
                        "routines_create__message": {"value": "ping"},
                    },
                }
            },
        },
    }


async def test_on_request_routines_create_view_submission_acks_clear_and_spawns_run() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()

    run_calls: list[dict[str, Any]] = []

    async def _fake_resolve_web_client(runtime: Any, *, team_id: str) -> MagicMock:
        return MagicMock()

    async def _fake_run(runtime: Any, wc: Any, **kwargs: Any) -> None:
        run_calls.append(kwargs)

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_rc_vs_ok_001",
        payload=_create_view_submission_payload(),
    )

    with (
        patch("daimon.adapters.slack.app.resolve_web_client", new=_fake_resolve_web_client),
        patch("daimon.adapters.slack.app.run_routines_create_submission", new=_fake_run),
    ):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "view_submission must ack first (before spawning the background run)"
    )
    ack_payload: dict[str, Any] = fake_client.sent_responses[0].payload or {}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    assert ack_payload.get("response_action") == "clear", (
        "valid routines__create submission must ack response_action=clear"
    )
    assert len(run_calls) == 1, "run_routines_create_submission must be spawned on proceed"
    assert run_calls[0].get("channel_id") == "C_FROM_META", (
        "channel_id must come from private_metadata, not the (absent) top-level channel"
    )


async def test_on_request_routines_create_block_action_acks_empty_then_dispatches() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()

    spawned: list[str] = []

    async def _fake_handle_routine_action(runtime: Any, payload: Any) -> None:
        spawned.append("routine_action")

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_rc_block_001",
        payload={
            "type": "block_actions",
            "team": {"id": "T_TEST"},
            "user": {"id": "U_TEST"},
            "trigger_id": "trig_rc_001",
            "view": {"id": "V_TEST", "private_metadata": "C_TEST"},
            "actions": [{"action_id": "routines_create"}],
        },
    )

    with patch(
        "daimon.adapters.slack.app.handle_routine_action",
        new=_fake_handle_routine_action,
    ):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "block_actions must ack empty first"
    )
    assert "routine_action" in spawned, (
        "routines_create block_action must spawn handle_routine_action"
    )
