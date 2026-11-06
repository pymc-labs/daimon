"""Where a routine may deliver for a member: the chat tool's reach, asked of the platform."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.web import channel_visibility
from daimon.adapters.web.channel_visibility import forget_visibility, postable_channels
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import DiscordSettings, Settings
from daimon.core.github_credentials import build_multifernet
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import web_settings

_VIEW, _SEND = 1 << 10, 1 << 11
_TENANT = uuid.uuid4()


def _runtime(handler: httpx.MockTransport) -> tuple[WebRuntime, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=handler)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        discord=DiscordSettings(bot_token=SecretStr("token")),
    )
    runtime = WebRuntime(
        settings=settings,
        web=web_settings(),
        sessionmaker=async_sessionmaker(class_=AsyncSession),
        http_client=http,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
    )
    return runtime, http


async def _postable(
    handler: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch, *, platform: str, ids: list[str]
) -> frozenset[str]:
    async def token(_runtime: object, _team: str) -> str:
        return "xoxb"

    monkeypatch.setattr(channel_visibility, "_slack_token", token)
    forget_visibility()
    runtime, http = _runtime(handler)
    async with http:
        return await postable_channels(
            runtime,
            tenant_id=_TENANT,
            platform=platform,
            workspace_id="W1",
            user_id="U1",
            channel_ids=ids,
        )


def _slack(
    *, bot: list[dict[str, object]], mine: list[str], guest: bool = False
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/users.info":
            return httpx.Response(200, json={"ok": True, "user": {"is_restricted": guest}})
        if "user" in request.url.params:
            return httpx.Response(200, json={"ok": True, "channels": [{"id": cid} for cid in mine]})
        return httpx.Response(200, json={"ok": True, "channels": bot})

    return httpx.MockTransport(handle)


async def test_slack_public_channel_daimon_is_in_needs_no_membership_from_a_full_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _slack(bot=[{"id": "C1", "is_private": False}], mine=[])
    found = await _postable(handler, monkeypatch, platform="slack", ids=["C1", "C2"])
    assert found == {"C1"}, "daimon is not in C2"


async def test_slack_private_channel_needs_the_callers_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = [{"id": "P1", "is_private": True}, {"id": "P2", "is_private": True}]
    handler = _slack(bot=bot, mine=["P2"])
    found = await _postable(handler, monkeypatch, platform="slack", ids=["P1", "P2"])
    assert found == {"P2"}


async def test_slack_guest_delivers_only_where_a_member(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = [{"id": "C1", "is_private": False}, {"id": "C2", "is_private": False}]
    handler = _slack(bot=bot, mine=["C2"], guest=True)
    found = await _postable(handler, monkeypatch, platform="slack", ids=["C1", "C2"])
    assert found == {"C2"}


def _discord(
    *, channels: list[dict[str, object]], member: dict[str, object], everyone: int
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/members/U1"):
            return httpx.Response(200, json={"roles": [], **member})
        if path.endswith("/channels"):
            return httpx.Response(200, json=channels)
        roles = [{"id": path.rsplit("/", 1)[-1], "permissions": str(everyone)}]
        return httpx.Response(200, json={"owner_id": "other", "roles": roles})

    return httpx.MockTransport(handle)


async def test_discord_member_timed_out_cannot_deliver(monkeypatch: pytest.MonkeyPatch) -> None:
    channels = [{"id": "T1", "type": 0, "permission_overwrites": []}]
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    timed_out = _discord(
        channels=channels, member={"communication_disabled_until": until}, everyone=_VIEW | _SEND
    )
    expired = _discord(
        channels=channels,
        member={"communication_disabled_until": "2020-01-01T00:00:00+00:00"},
        everyone=_VIEW | _SEND,
    )
    assert await _postable(timed_out, monkeypatch, platform="discord", ids=["T1"]) == frozenset()
    assert await _postable(expired, monkeypatch, platform="discord", ids=["T1"]) == {"T1"}


async def test_discord_delivers_only_to_text_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    channels = [
        {"id": "T1", "type": 0, "permission_overwrites": []},
        {"id": "N1", "type": 5, "permission_overwrites": []},
        {"id": "F1", "type": 15, "permission_overwrites": []},
        {"id": "V1", "type": 2, "permission_overwrites": []},
    ]
    handler = _discord(channels=channels, member={}, everyone=_VIEW | _SEND)
    found = await _postable(handler, monkeypatch, platform="discord", ids=["T1", "N1", "F1", "V1"])
    assert found == {"T1", "N1"}, "forum and voice channels are not text channels"
