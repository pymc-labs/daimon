"""Slack remembers the names a payload carries, in the background, after the ack."""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from anthropic import AsyncAnthropic
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.names import payload_name, remember_payload_names
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core import platform_names
from daimon.core.platform_names import KnownName
from daimon.core.stores.platform_names import get_user_names
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MESSAGE = {
    "team_id": "T1",
    "event": {
        "type": "app_mention",
        "user": "U0MAYA",
        "user_profile": {"display_name": "", "real_name": "Maya Chen", "name": "maya"},
    },
}


def test_each_payload_kind_names_its_person() -> None:
    assert payload_name("events_api", _MESSAGE) == ("T1", "U0MAYA", "Maya Chen", "maya"), (
        "a message's profile: display name, else real name"
    )
    slash = {"team_id": "T1", "user_id": "U0MAYA", "user_name": "maya"}
    assert payload_name("slash_commands", slash) == ("T1", "U0MAYA", None, "maya")
    click = {"team": {"id": "T1"}, "user": {"id": "U0MAYA", "username": "maya", "name": "maya"}}
    assert payload_name("interactive", click) == ("T1", "U0MAYA", None, "maya")
    bare = {"team_id": "T1", "event": {"type": "app_mention", "user": "U0MAYA"}}
    assert payload_name("events_api", bare) == ("T1", "U0MAYA", None, None), "no profile, no name"
    assert payload_name("hello", {}) == ("", "", None, None)


async def test_a_payloads_name_is_stored(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()

    remember_payload_names(db_session_factory, "events_api", _MESSAGE)
    await platform_names.settle()  # the test's sessions share one connection
    remember_payload_names(
        db_session_factory,
        "slash_commands",
        {"team_id": "T1", "user_id": "U0MAYA", "user_name": "m2"},
    )
    await platform_names.settle()

    stored = await get_user_names(
        db_session, tenant_id=tenant.id, platform="slack", user_ids=["U0MAYA"]
    )
    assert stored == {"U0MAYA": KnownName("Maya Chen", "m2")}, (
        "a later handle-only sighting keeps the display name"
    )


@dataclasses.dataclass
class _FakeSocketClient:
    sent: list[SocketModeResponse] = dataclasses.field(default_factory=list[SocketModeResponse])

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.sent.append(response)


async def test_on_request_remembers_after_the_ack_and_a_failure_never_reaches_it() -> None:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr("dummykey"),)
    settings.slack.max_concurrent_turns_per_tenant = 3
    broken = MagicMock(side_effect=OSError("database unreachable"))
    app = SlackApp(
        runtime=SlackRuntime(
            settings=settings,
            anthropic=MagicMock(spec=AsyncAnthropic),
            sessionmaker=broken,
            billing_config=None,
            http_client=MagicMock(spec=httpx.AsyncClient),
            resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub
            turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub
        )
    )
    client = _FakeSocketClient()
    payload: dict[str, Any] = {"team_id": "T9", "user_id": "U0SAM", "user_name": "sam"}
    payload |= {"command": "/nothing-here", "channel_id": "C1", "trigger_id": "x"}

    with patch("daimon.adapters.slack.app.remember_payload_names") as remember:
        await app.on_request(client, SocketModeRequest("slash_commands", "env1", payload))  # type: ignore[arg-type]
    remember.assert_called_once_with(broken, "slash_commands", payload)
    assert client.sent, "the ack went out"

    remember_payload_names(broken, "slash_commands", payload)
    await platform_names.settle()  # the failed write is logged, never raised
