"""SlackApp routes the /billing redeem button and form submission."""

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
    sent: list[SocketModeResponse] = dataclasses.field(default_factory=list[SocketModeResponse])

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.call_log.append("ack")
        self.sent.append(response)


def _app() -> SlackApp:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr("dummykey"),)
    settings.slack.max_concurrent_turns_per_tenant = 3
    return SlackApp(
        runtime=SlackRuntime(
            settings=settings,
            anthropic=MagicMock(spec=AsyncAnthropic),
            sessionmaker=MagicMock(),
            billing_config=None,
            http_client=MagicMock(spec=httpx.AsyncClient),
            resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
            turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        )
    )


async def _drain(app: SlackApp) -> None:
    pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_redeem_submission_acks_first_then_redeems_in_the_background() -> None:
    """The form submission is acked before the redemption runs in the background."""
    client, app, calls = _FakeSocketClient(), _app(), list[str]()

    async def _fake_client(runtime: Any, *, team_id: str) -> MagicMock:
        return MagicMock()

    async def _fake_run(runtime: Any, wc: Any, **kwargs: Any) -> None:
        # a failure here leaves ``calls`` empty
        assert client.call_log == ["ack"], "the ack should go out before redeeming"
        calls.append(kwargs["decision"].code)

    payload = {
        "type": "view_submission",
        "team": {"id": "T"},
        "user": {"id": "U"},
        "view": {
            "callback_id": "billing_redeem",
            "id": "V_FORM",
            "private_metadata": json.dumps({"root_view_id": "V_ROOT"}),
            "state": {"values": {"billing_redeem_code": {"code": {"value": "WELCOME-2026"}}}},
        },
    }
    with (
        patch("daimon.adapters.slack.app.resolve_web_client", new=_fake_client),
        patch("daimon.adapters.slack.app.run_redeem_submission", new=_fake_run),
    ):
        await app.on_request(client, SocketModeRequest("interactive", "env1", payload))  # type: ignore[arg-type]
        await _drain(app)

    ack: dict[str, Any] = client.sent[0].payload or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    assert ack.get("response_action") == "update", "the ack should update the form in place"
    assert calls == ["WELCOME-2026"], "the submitted code should be redeemed"


async def test_redeem_button_is_routed() -> None:
    """The redeem button is acked and handed to the redeem-form opener."""
    client, app, calls = _FakeSocketClient(), _app(), list[str]()

    async def _fake_open(runtime: Any, payload: dict[str, Any]) -> None:
        calls.append(payload["actions"][0]["action_id"])

    payload = {
        "type": "block_actions",
        "team": {"id": "T"},
        "user": {"id": "U"},
        "actions": [{"action_id": "billing_redeem_open", "type": "button"}],
    }
    with patch("daimon.adapters.slack.app.handle_redeem_open", new=_fake_open):
        await app.on_request(client, SocketModeRequest("interactive", "env2", payload))  # type: ignore[arg-type]
        await _drain(app)

    assert client.call_log[0] == "ack", "the button press should be acked first"
    assert calls == ["billing_redeem_open"], "the button should open the redeem form"
