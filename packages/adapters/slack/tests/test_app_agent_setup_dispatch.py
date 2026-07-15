"""Dispatch tests for agent_setup__* routes in SlackApp.on_request (83-07).

Covers:
- /agent-setup slash command routes to handle_agent_setup_command
- agent_setup__new_agent view_submission: ack-with-payload before any I/O (STURN-01);
  invalid name → errors acked, no background run spawns
- agent_setup__roster_select block_action: empty ack first, then handler dispatches
- Existing routes regression guard (no agent_setup changes break prior routes)
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

# ---------------------------------------------------------------------------
# Shared fakes (transport-level discipline — no method-level mocks on web client)
# ---------------------------------------------------------------------------


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
    """Build a minimal SlackApp for dispatch tests (no real DB needed)."""
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
    """Await all background tasks spawned by the app."""
    pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# /agent-setup slash command routing
# ---------------------------------------------------------------------------


async def test_on_request_agent_setup_slash_when_command_arrives_spawns_handler() -> None:
    """/agent-setup slash command spawns handle_agent_setup_command (not a no-op)."""
    fake_client = _FakeSocketClient()
    app = _make_app()

    spawned_cmds: list[str] = []

    async def _fake_agent_setup_command(runtime: Any, payload: Any) -> None:
        spawned_cmds.append("agent_setup")

    req = SocketModeRequest(
        type="slash_commands",
        envelope_id="env_agent_setup_slash_001",
        payload={
            "command": "/agent-setup",
            "team_id": "T_TEST",
            "user_id": "U_TEST",
            "channel_id": "C_TEST",
            "trigger_id": "trig_agent_setup_001",
        },
    )

    with patch(
        "daimon.adapters.slack.app.handle_agent_setup_command",
        new=_fake_agent_setup_command,
    ):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "/agent-setup slash must ack first (send_socket_mode_response before spawning the handler)"
    )
    assert "agent_setup" in spawned_cmds, (
        "handle_agent_setup_command must be spawned for a /agent-setup slash_commands envelope"
    )


# ---------------------------------------------------------------------------
# view_submission ack-before-I/O ordering + error path (STURN-01)
# ---------------------------------------------------------------------------


def _make_new_agent_view_submission_payload(*, name: str) -> dict[str, Any]:
    """Build a minimal agent_setup__new_agent view_submission payload."""
    return {
        "type": "view_submission",
        "team": {"id": "T_TEST"},
        "user": {"id": "U_TEST"},
        "view": {
            "callback_id": "agent_setup__new_agent",
            "id": "V_TEST",
            "private_metadata": json.dumps(
                {
                    "team_id": "T_TEST",
                    "channel_id": "C_TEST",
                }
            ),
            "state": {
                "values": {
                    "new_agent__name": {
                        "new_agent__name": {"value": name},
                    },
                    "new_agent__model": {
                        "new_agent__model": {"value": ""},
                    },
                    "new_agent__prompt": {
                        "new_agent__prompt": {"value": ""},
                    },
                }
            },
        },
    }


async def test_on_request_new_agent_view_submission_when_invalid_name_acks_errors_and_no_run_spawns() -> (
    None
):
    """view_submission agent_setup__new_agent with invalid name:
    - send_socket_mode_response is called first (ack before I/O, STURN-01)
    - ack payload carries response_action=errors
    - no background run spawns (proceed=False)
    """
    fake_client = _FakeSocketClient()
    app = _make_app()

    # Invalid name: contains spaces — fails _AGENT_NAME_RE
    payload = _make_new_agent_view_submission_payload(name="invalid name with spaces")

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_new_agent_vs_error_001",
        payload=payload,
    )

    run_calls: list[str] = []

    async def _fake_run_new_agent(runtime: Any, wc: Any, **kwargs: Any) -> None:
        run_calls.append("run_new_agent")

    with patch("daimon.adapters.slack.app.run_new_agent_submission", new=_fake_run_new_agent):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "view_submission must ack first — send_socket_mode_response before any background I/O (STURN-01)"
    )
    assert len(fake_client.sent_responses) == 1, (
        "exactly one SocketModeResponse must be sent for a view_submission"
    )
    ack_payload: dict[str, Any] = fake_client.sent_responses[0].payload or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    assert ack_payload.get("response_action") == "errors", (
        "invalid agent name must ack with response_action=errors — modal stays open"
    )
    assert not run_calls, (
        "run_new_agent_submission must NOT be spawned when proceed=False (validation failed)"
    )


async def test_on_request_new_agent_view_submission_when_valid_name_acks_clear_and_spawns_run() -> (
    None
):
    """view_submission agent_setup__new_agent with valid name:
    - ack carries response_action=clear (modal dismissed)
    - background run is spawned after the ack (I/O after ack, STURN-01)
    """
    fake_client = _FakeSocketClient()
    app = _make_app()

    payload = _make_new_agent_view_submission_payload(name="my-valid-agent")

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_new_agent_vs_ok_001",
        payload=payload,
    )

    run_calls: list[str] = []

    async def _fake_resolve_web_client(runtime: Any, *, team_id: str) -> MagicMock:
        return MagicMock()

    async def _fake_run_new_agent(runtime: Any, wc: Any, **kwargs: Any) -> None:
        run_calls.append("run_new_agent")

    with (
        patch(
            "daimon.adapters.slack.app.resolve_web_client",
            new=_fake_resolve_web_client,
        ),
        patch(
            "daimon.adapters.slack.app.run_new_agent_submission",
            new=_fake_run_new_agent,
        ),
    ):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "view_submission must ack first — ack before spawning the background run (STURN-01)"
    )
    ack_payload: dict[str, Any] = fake_client.sent_responses[0].payload or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    assert ack_payload.get("response_action") == "clear", (
        "valid name must ack with response_action=clear (modal dismissed)"
    )
    assert "run_new_agent" in run_calls, (
        "run_new_agent_submission must be spawned when proceed=True (validation passed)"
    )


async def test_on_request_new_agent_view_submission_sources_channel_id_from_private_metadata() -> (
    None
):
    """The background run must receive channel_id from the view's private_metadata.

    view_submission payloads carry no top-level "channel"; reading payload["channel"]
    yields "" and every chat_postEphemeral 500s with channel_not_found. The invoking
    channel lives in private_metadata — assert it is threaded through to the run.
    """
    fake_client = _FakeSocketClient()
    app = _make_app()

    payload = _make_new_agent_view_submission_payload(name="my-valid-agent")

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_new_agent_vs_chan_001",
        payload=payload,
    )

    captured_kwargs: dict[str, Any] = {}

    async def _fake_resolve_web_client(runtime: Any, *, team_id: str) -> MagicMock:
        return MagicMock()

    async def _fake_run_new_agent(runtime: Any, wc: Any, **kwargs: Any) -> None:
        captured_kwargs.update(kwargs)

    with (
        patch(
            "daimon.adapters.slack.app.resolve_web_client",
            new=_fake_resolve_web_client,
        ),
        patch(
            "daimon.adapters.slack.app.run_new_agent_submission",
            new=_fake_run_new_agent,
        ),
    ):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert captured_kwargs.get("channel_id") == "C_TEST", (
        "channel_id must be sourced from private_metadata (C_TEST), not the empty "
        f"top-level payload channel, got {captured_kwargs.get('channel_id')!r}"
    )


# ---------------------------------------------------------------------------
# block_actions — empty ack first, then handler dispatch
# ---------------------------------------------------------------------------


async def test_on_request_roster_select_block_action_when_arrives_acks_empty_first_then_dispatches() -> (
    None
):
    """agent_setup__roster_select block_action:
    - the unconditional empty ack fires first (STURN-01)
    - handle_agent_setup_action is spawned
    """
    fake_client = _FakeSocketClient()
    app = _make_app()

    spawned_actions: list[str] = []

    async def _fake_handle_action(runtime: Any, payload: Any) -> None:
        spawned_actions.append("agent_setup_action")

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_roster_select_001",
        payload={
            "type": "block_actions",
            "team": {"id": "T_TEST"},
            "user": {"id": "U_TEST"},
            "view": {
                "id": "V_TEST",
                "private_metadata": json.dumps({"team_id": "T_TEST", "channel_id": "C_TEST"}),
            },
            "actions": [
                {
                    "action_id": "agent_setup__roster_select",
                    "selected_option": {"value": "my-agent"},
                }
            ],
        },
    )

    with patch(
        "daimon.adapters.slack.app.handle_agent_setup_action",
        new=_fake_handle_action,
    ):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "block_actions must ack empty envelope first — before any handler dispatch (STURN-01)"
    )
    assert "agent_setup_action" in spawned_actions, (
        "handle_agent_setup_action must be spawned for an agent_setup__roster_select block_action"
    )


# ---------------------------------------------------------------------------
# Regression guard — existing routes unchanged
# ---------------------------------------------------------------------------


async def test_on_request_help_slash_when_command_arrives_still_routes_correctly() -> None:
    """/help slash command still routes to handle_help_command after agent_setup wiring.

    Regression guard: the new /agent-setup branch must not displace existing routes.
    """
    fake_client = _FakeSocketClient()
    app = _make_app()

    spawned_cmds: list[str] = []

    async def _fake_help_command(runtime: Any, payload: Any) -> None:
        spawned_cmds.append("help")

    req = SocketModeRequest(
        type="slash_commands",
        envelope_id="env_help_regression_001",
        payload={
            "command": "/help",
            "team_id": "T_TEST",
            "user_id": "U_TEST",
            "channel_id": "C_TEST",
            "trigger_id": "trig_help_001",
        },
    )

    with patch("daimon.adapters.slack.app.handle_help_command", new=_fake_help_command):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "/help must still ack first after agent_setup wiring"
    )
    assert "help" in spawned_cmds, (
        "/help slash must still route to handle_help_command (regression guard)"
    )
