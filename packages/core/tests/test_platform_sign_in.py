"""Chat-platform sign-in helpers: Discord user and guilds, Slack user grant."""

from __future__ import annotations

import httpx
import pytest
from daimon.core.platform_sign_in import (
    PlatformSignInError,
    exchange_slack_user_code,
    fetch_discord_user,
    fetch_discord_workspaces,
)


def _discord_http(*, user: dict[str, str], guilds: list[tuple[str, str]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer upstream-tok", request.headers
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(200, json=user)
        if request.url.path == "/api/v10/users/@me/guilds":
            return httpx.Response(200, json=[{"id": g, "name": n} for g, n in guilds])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _slack_http(payload: dict[str, object]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/oauth.v2.access", request.url
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_discord_workspaces_returns_user_and_guilds() -> None:
    http = _discord_http(user={"id": "u1", "username": "j"}, guilds=[("g1", "PyMC"), ("g2", "B")])
    user, guilds = await fetch_discord_workspaces(http, access_token="upstream-tok")
    assert user == "u1", f"got {user!r}"
    assert guilds == [("g1", "PyMC"), ("g2", "B")], f"got {guilds!r}"


async def test_fetch_discord_user_reads_every_page_of_guilds() -> None:
    """A user in more than 200 servers keeps all of them: the next page starts `after` the last."""
    everything = [(str(1000 + i), f"G{i}") for i in range(450)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": "u1", "username": "j"})
        after = request.url.params.get("after")
        start = 0 if after is None else int(after) - 1000 + 1
        page = everything[start : start + int(request.url.params["limit"])]
        return httpx.Response(200, json=[{"id": g, "name": n} for g, n in page])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    user = await fetch_discord_user(http, access_token="upstream-tok")
    assert user.workspaces == everything, f"got {len(user.workspaces)} of {len(everything)} guilds"


async def test_fetch_discord_user_prefers_the_display_name() -> None:
    http = _discord_http(user={"id": "u1", "username": "j", "global_name": "Jo"}, guilds=[])
    user = await fetch_discord_user(http, access_token="upstream-tok")
    assert user.name == "Jo", f"the display name beats the username, got {user.name!r}"


async def test_slack_exchange_lifts_the_user_grant() -> None:
    http = _slack_http(
        {
            "ok": True,
            "team": {"id": "T1", "name": "Acme"},
            "authed_user": {"id": "U1", "access_token": "xoxp-user", "scope": "users:read"},
        }
    )
    grant = await exchange_slack_user_code(
        http, client_id="c", client_secret="s", code="code", redirect_uri="https://r.example"
    )
    assert (grant.team_id, grant.team_name, grant.user_id) == ("T1", "Acme", "U1"), grant
    assert grant.access_token == "xoxp-user" and grant.expires_in is None, grant


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"ok": False, "error": "invalid_code"}, "invalid_code"),
        ({"ok": True, "is_enterprise_install": True, "authed_user": {}}, "Enterprise Grid"),
        ({"ok": True, "authed_user": {"id": "U1"}}, "no user token"),
    ],
)
async def test_slack_exchange_refuses_unusable_answers(
    payload: dict[str, object], match: str
) -> None:
    with pytest.raises(PlatformSignInError, match=match):
        await exchange_slack_user_code(
            _slack_http(payload), client_id="c", client_secret="s", code="x", redirect_uri="r"
        )
