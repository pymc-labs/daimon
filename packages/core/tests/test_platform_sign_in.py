"""Chat-platform sign-in helpers: Discord user and guilds, Slack grant and name, Microsoft account."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from daimon.core.config import MicrosoftAccountsSettings
from daimon.core.microsoft_identity import MicrosoftIdentityMismatch, MicrosoftTokens
from daimon.core.platform_sign_in import (
    PlatformSignInError,
    exchange_slack_user_code,
    fetch_discord_user,
    fetch_discord_workspaces,
    fetch_slack_user_name,
    slack_authorize_url,
    verify_microsoft_sign_in,
)
from pydantic import HttpUrl, SecretStr

TENANT = "11111111-1111-1111-1111-111111111111"
OID = "22222222-2222-2222-2222-222222222222"
MICROSOFT = MicrosoftAccountsSettings(
    client_id="app-1",
    client_secret=SecretStr("app-secret"),
    tenant_id=TENANT,
    authority=HttpUrl("https://login.example.com"),
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


def _microsoft_tokens(**claims: object) -> MicrosoftTokens:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return MicrosoftTokens(
        access_token="at", refresh_token=None, expires_in=3600, scope="", id_token=f"e30.{body}.s"
    )


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


def test_slack_authorize_url_asks_for_a_user_grant_only() -> None:
    url = urlsplit(
        slack_authorize_url(client_id="c", redirect_uri="https://d.example/cb", state="st")
    )
    query = parse_qs(url.query)
    assert (url.netloc, url.path) == ("slack.com", "/oauth/v2/authorize"), url
    assert query["user_scope"] == ["users:read"], "identity only"
    assert "scope" not in query, "a bot scope would install the app into the workspace"
    assert query["state"] == ["st"] and query["redirect_uri"] == ["https://d.example/cb"], query
    assert "code_challenge" not in query, "Slack takes no PKCE"


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ({"profile": {"display_name": "ines"}, "real_name": "Ines R", "name": "ir"}, "ines"),
        ({"profile": {"display_name": ""}, "real_name": "Ines R", "name": "ir"}, "Ines R"),
        ({"profile": {}, "name": "ir"}, "ir"),
        ({}, None),
    ],
)
async def test_fetch_slack_user_name_prefers_display_then_real_then_handle(
    user: dict[str, object], expected: str | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/users.info", request.url
        assert request.url.params["user"] == "U1", request.url
        assert request.headers["Authorization"] == "Bearer xoxp-1", request.headers
        return httpx.Response(200, json={"ok": True, "user": user})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    name = await fetch_slack_user_name(http, access_token="xoxp-1", user_id="U1")
    assert name == expected, f"got {name!r}"


async def test_fetch_slack_user_name_is_none_when_slack_refuses() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"ok": False, "error": "missing_scope"})
        )
    )
    name = await fetch_slack_user_name(http, access_token="xoxp-1", user_id="U1")
    assert name is None, "a refusal falls back to the caller's default, it does not raise"


def test_microsoft_sign_in_returns_the_canonical_directory_and_object_ids() -> None:
    tokens = _microsoft_tokens(
        tid=TENANT.upper(), oid=OID.upper(), aud="app-1", name="Ines", preferred_username="i@x"
    )
    signed = verify_microsoft_sign_in(tokens, MICROSOFT)
    assert (signed.tenant_id, signed.object_id, signed.name) == (TENANT, OID, "Ines"), signed


@pytest.mark.parametrize(
    "claims",
    [
        {"tid": "33333333-3333-3333-3333-333333333333", "oid": OID, "aud": "app-1"},
        {"tid": TENANT, "oid": OID, "aud": "other-app"},
        {"tid": TENANT, "aud": "app-1"},
    ],
    ids=["other-tenant", "other-app", "no-object-id"],
)
def test_microsoft_sign_in_refuses_foreign_directories_apps_and_missing_ids(
    claims: dict[str, str],
) -> None:
    with pytest.raises(MicrosoftIdentityMismatch):
        verify_microsoft_sign_in(_microsoft_tokens(**claims), MICROSOFT)


def test_microsoft_sign_in_refuses_a_response_without_an_id_token() -> None:
    tokens = MicrosoftTokens(
        access_token="at", refresh_token=None, expires_in=3600, scope="", id_token=None
    )
    with pytest.raises(MicrosoftIdentityMismatch):
        verify_microsoft_sign_in(tokens, MICROSOFT)
