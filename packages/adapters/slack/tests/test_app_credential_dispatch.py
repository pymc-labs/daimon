"""Dispatch tests for the credential-request routes in SlackApp.on_request.

Covers:
- credential_request block_action: empty ack first, then the click handler
  is spawned with the payload.
- credential_request__env view_submission: ack carries the field-error
  payload for an empty value (no background run spawns), and an empty ack
  plus a spawned runner for a valid value.
- credential_request__env_file view_submission routes the uploaded file's id
  to the env_file runner.
- The continuation trigger handed to every runner is addressed from the
  request row, so it reaches the thread the request was minted in.
- An external Slack Connect click never reaches the handler.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from anthropic import AsyncAnthropic
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.credential_requests import mint_request_token
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.stores.credential_requests import create_credential_request
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclasses.dataclass
class _FakeSocketClient:
    call_log: list[str] = dataclasses.field(default_factory=list[str])
    sent_responses: list[SocketModeResponse] = dataclasses.field(
        default_factory=list[SocketModeResponse]
    )

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.call_log.append("send_socket_mode_response")
        self.sent_responses.append(response)


def _make_app(sessionmaker: Any = None) -> SlackApp:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr("dummykey"),)
    settings.slack.max_concurrent_turns_per_tenant = 3
    runtime = SlackRuntime(
        settings=settings,
        anthropic=MagicMock(spec=AsyncAnthropic),
        sessionmaker=sessionmaker if sessionmaker is not None else MagicMock(),
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


_TEAM_ID = "T_TEST"
_ORIGIN_CHANNEL = "C_ORIGIN"
_ORIGIN_THREAD = "1700000009.000900"


def _env_file_submission_payload(file_id: str) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": _TEAM_ID},
        "user": {"id": "U_TEST"},
        "view": {
            "callback_id": "credential_request__env_file",
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
                    "credential__file": {
                        "credential__file": {
                            "type": "file_input",
                            "files": [{"id": file_id, "name": ".env", "size": 32}],
                        }
                    }
                }
            },
        },
    }


async def test_env_file_submission_routes_to_the_env_file_runner() -> None:
    fake_client = _FakeSocketClient()
    app = _make_app()
    ran: list[dict[str, Any]] = []

    async def _fake_run(runtime: Any, **kwargs: Any) -> None:
        ran.append(kwargs)

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_cred_submit_file",
        payload=_env_file_submission_payload("F_UPLOAD"),
    )
    with patch("daimon.adapters.slack.app.run_env_file_credential_submission", new=_fake_run):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)

    assert not fake_client.sent_responses[0].payload, (
        "a valid upload must ack empty (close the modal)"
    )
    assert len(ran) == 1, "the env_file kind has its own runner and must reach it"
    assert ran[0]["file_id"] == "F_UPLOAD", "the runner is handed the uploaded file's id"
    assert "value" not in ran[0], "an uploaded file is a handle, not a pasted value"


async def test_dispatch_trigger_targets_the_requests_origin_thread(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The form carries routing handles only, so the thread a saved value
    unblocks is read off the request row, not off the submission."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM_ID)
    await make_account(db_session, tenant=tenant, id=derive_guild_account_uuid(tenant_id=tenant.id))
    token = mint_request_token()
    await create_credential_request(
        db_session,
        token=token,
        kind="env",
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=derive_guild_account_uuid(tenant_id=tenant.id),
        target="OPENAI_API_KEY",
        mcp_server_url=None,
        requester_platform_user_id="U_TEST",
        channel_id=_ORIGIN_CHANNEL,
        platform="slack",
        parent_channel_id=_ORIGIN_CHANNEL,
        origin_thread_id=_ORIGIN_THREAD,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="tester",
        requested_work="finish the report",
    )
    await db_session.commit()

    fake_client = _FakeSocketClient()
    app = _make_app(db_session_factory)
    captured: list[dict[str, Any]] = []
    dispatched: list[dict[str, Any]] = []

    async def _fake_run(runtime: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    async def _fake_dispatch(self: SlackApp, **kwargs: Any) -> None:
        dispatched.append(kwargs)

    payload = _submission_payload("s3cr3t")
    payload["view"]["private_metadata"] = json.dumps(
        {"token": token, "channel_id": "C_ELSEWHERE", "message_ts": "1.2"},
        separators=(",", ":"),
    )
    req = SocketModeRequest(type="interactive", envelope_id="env_cred_trigger", payload=payload)
    with (
        patch("daimon.adapters.slack.app.run_env_credential_submission", new=_fake_run),
        patch.object(SlackApp, "dispatch_continuations_in_thread", new=_fake_dispatch),
        patch("daimon.adapters.slack.app.resolve_web_client", return_value=MagicMock()),
    ):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        await _drain(app)
        assert len(captured) == 1, "the valid submission must reach the runner"
        await captured[0]["dispatch_continuations"]()

    assert len(dispatched) == 1, "calling the trigger must dispatch the thread's continuations"
    assert dispatched[0]["thread_id"] == _ORIGIN_THREAD, (
        "the queued turn belongs in the thread the request was minted in, not the "
        "channel the form was submitted from"
    )
    assert dispatched[0]["channel"] == _ORIGIN_CHANNEL
    assert dispatched[0]["tenant_id"] == tenant.id
    assert dispatched[0]["account_id"] == derive_guild_account_uuid(tenant_id=tenant.id)
