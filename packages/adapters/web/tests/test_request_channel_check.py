"""Can Daimon post in the support request channel: asked of the platform, with REST faked."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.web.channel_visibility import forget_visibility, request_channel_check
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import channel_post_check
from daimon.core.config import DiscordSettings, Settings
from daimon.core.github_credentials import build_multifernet
from daimon.core.support_channel_check import ChannelCheck
from daimon.core.support_routing import SupportDestination
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import web_settings

_VIEW, _SEND, _EMBED = 1 << 10, 1 << 11, 1 << 14
_ALL = _VIEW | _SEND | _EMBED
_UNVERIFIED = ChannelCheck("unverified", "Couldn't check. Try again.")
_READY = ChannelCheck("ready")

type Handler = Callable[[httpx.Request], httpx.Response]


def _runtime(handler: Handler, *, discord: bool = True) -> tuple[WebRuntime, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        discord=DiscordSettings(bot_token=SecretStr("token")) if discord else None,
    )
    runtime = WebRuntime(
        settings=settings,
        web=web_settings(),
        sessionmaker=async_sessionmaker(class_=AsyncSession),
        http_client=http,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
    )
    return runtime, http


async def _check(
    handler: Handler,
    destination: SupportDestination,
    monkeypatch: pytest.MonkeyPatch,
    *,
    discord: bool = True,
) -> ChannelCheck:
    async def token(*_args: object) -> str:
        return "xoxb"

    monkeypatch.setattr(channel_post_check, "slack_token", token)
    forget_visibility()
    runtime, http = _runtime(handler, discord=discord)
    async with http:
        return await request_channel_check(runtime, destination, label="#ask")


def _discord(
    *,
    everyone: int = _ALL,
    overwrites: Sequence[Mapping[str, object]] | None = None,
    channel: dict[str, object] | None = None,
    channel_status: int = 200,
    guild_status: int = 200,
) -> Handler:
    """Guild G1 where Daimon (B1) holds only @everyone's permissions, and channel C1."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "B1"})
        if path.endswith("/channels/C1"):
            body = {
                "id": "C1",
                "type": 0,
                "guild_id": "G1",
                "permission_overwrites": overwrites or [],
            }
            return httpx.Response(channel_status, json={**body, **(channel or {})})
        if path.endswith("/members/B1"):
            return httpx.Response(200, json={"roles": []})
        roles = [{"id": "G1", "permissions": str(everyone)}]
        return httpx.Response(guild_status, json={"owner_id": "other", "roles": roles})

    return handle


_DISCORD_DEST = SupportDestination(platform="discord", channel_id="C1", own=True)


async def test_discord_is_ready_when_daimon_can_view_send_and_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = await _check(_discord(), _DISCORD_DEST, monkeypatch)
    assert found == _READY, "all three permissions held"


async def test_discord_names_the_permission_the_channel_overwrite_takes_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deny_embed = [{"id": "G1", "type": 0, "allow": "0", "deny": str(_EMBED)}]
    found = await _check(_discord(overwrites=deny_embed), _DISCORD_DEST, monkeypatch)
    assert found == ChannelCheck("blocked", "Give Daimon Embed Links in #ask."), found


async def test_discord_names_every_missing_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    found = await _check(_discord(everyone=_VIEW), _DISCORD_DEST, monkeypatch)
    assert found == ChannelCheck("blocked", "Give Daimon Send Messages and Embed Links in #ask."), (
        found
    )


async def test_discord_channel_daimon_cannot_see_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = await _check(_discord(channel_status=403), _DISCORD_DEST, monkeypatch)
    assert found == ChannelCheck("blocked", "Daimon can't see #ask. Give it View Channel."), found


async def test_discord_missing_channel_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    found = await _check(_discord(channel_status=404), _DISCORD_DEST, monkeypatch)
    assert found == ChannelCheck("blocked", "Daimon can't see #ask. Give it View Channel."), found


@pytest.mark.parametrize("kind", [2, 5, 11])
async def test_discord_voice_announcement_and_thread_are_not_a_text_channel(
    kind: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = await _check(_discord(channel={"type": kind}), _DISCORD_DEST, monkeypatch)
    assert found == ChannelCheck("blocked", "#ask isn't a text channel."), found


async def test_discord_platform_errors_are_unverified_never_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = await _check(_discord(guild_status=500), _DISCORD_DEST, monkeypatch)

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    unreachable = await _check(down, _DISCORD_DEST, monkeypatch)
    no_token = await _check(_discord(), _DISCORD_DEST, monkeypatch, discord=False)
    assert [refused, unreachable, no_token] == [_UNVERIFIED] * 3, "each failure is unverified"


def _slack(channel: dict[str, object] | None = None, *, error: str | None = None) -> Handler:
    def handle(_request: httpx.Request) -> httpx.Response:
        if error is not None:
            return httpx.Response(200, json={"ok": False, "error": error})
        return httpx.Response(200, json={"ok": True, "channel": {"id": "C1", **(channel or {})}})

    return handle


_SLACK_DEST = SupportDestination(platform="slack", channel_id="C1", own=True, slack_team_id="T1")


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (_slack({"is_member": False, "is_private": False}), _READY),
        (_slack({"is_member": True, "is_private": True}), _READY),
        (
            _slack({"is_member": False, "is_private": True}),
            ChannelCheck("blocked", "Invite Daimon to #ask."),
        ),
        (_slack(error="channel_not_found"), ChannelCheck("blocked", "Invite Daimon to #ask.")),
        (_slack({"is_archived": True}), ChannelCheck("blocked", "#ask is archived.")),
        (_slack(error="ratelimited"), _UNVERIFIED),
        (_slack(error="missing_scope"), _UNVERIFIED),
    ],
)
async def test_slack_public_is_ready_private_needs_an_invite_and_errors_are_unverified(
    handler: Handler, expected: ChannelCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = await _check(handler, _SLACK_DEST, monkeypatch)
    assert found == expected, found


async def test_slack_without_a_team_to_find_the_token_is_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = SupportDestination(platform="slack", channel_id="C1", own=False)
    assert await _check(_slack(), destination, monkeypatch) == _UNVERIFIED


async def test_teams_pick_is_ready_only_when_it_carries_team_and_service_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Teams needs no REST call")

    def teams(**fields: str | bool) -> SupportDestination:
        return SupportDestination(platform="teams", channel_id="19:c", **{"own": True, **fields})  # pyright: ignore[reportArgumentType]

    full = teams(teams_team_id="19:t", teams_service_url="https://smba.example.com/")
    no_url = teams(teams_team_id="19:t")
    bare = teams()
    deployment = teams(own=False)
    found = [await _check(never, d, monkeypatch) for d in (full, no_url, bare, deployment)]
    assert found == [
        _READY,
        ChannelCheck("blocked", "Pick #ask again to record its service URL."),
        ChannelCheck("blocked", "Pick #ask again to record its team id and service URL."),
        _READY,
    ], found


async def test_an_answer_is_cached_for_a_minute_and_fresh_asks_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    inner = _discord()

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return inner(request)

    forget_visibility()
    runtime, http = _runtime(counting)
    async with http:
        first = await request_channel_check(runtime, _DISCORD_DEST, label="#ask")
        after_first = len(calls)
        again = await request_channel_check(runtime, _DISCORD_DEST, label="#ask")
        cached = len(calls)
        await request_channel_check(runtime, _DISCORD_DEST, label="#ask", fresh=True)

    assert first == again == _READY, "same answer"
    assert cached == after_first, "the second ask made no REST call"
    assert len(calls) == 2 * after_first, "fresh asked the platform again"


async def test_an_unverified_answer_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = True
    inner = _discord()

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500) if failing else inner(request)

    forget_visibility()
    runtime, http = _runtime(flaky)
    async with http:
        first = await request_channel_check(runtime, _DISCORD_DEST, label="#ask")
        failing = False
        second = await request_channel_check(runtime, _DISCORD_DEST, label="#ask")

    assert (first, second) == (_UNVERIFIED, _READY), "a failure must not stick for a minute"
