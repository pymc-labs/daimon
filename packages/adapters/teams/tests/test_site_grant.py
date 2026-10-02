"""Tests for the Teams site grant: signed state, the sign-in URL, and the three Graph calls."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from daimon.adapters.teams import site_grant
from daimon.adapters.teams.site_grant import (
    CALLBACK_PATH,
    STATE_TTL_S,
    SiteGrantFailed,
    authorize_url,
    callback_route,
    grant_team_site,
    sign_state,
    verify_state,
)
from fastapi import FastAPI

from .conftest import teams_settings

_GROUP = "0f6b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d"
_SECRET = "client-secret"
_NOW = 1_800_000_000.0
_SITE_ID = "example.sharepoint.com,1111,2222"


def test_state_round_trips_the_group_id() -> None:
    """A fresh state verifies back to the group it was signed for."""
    state = sign_state(_GROUP, secret=_SECRET, now=_NOW)
    assert verify_state(state, secret=_SECRET, now=_NOW + 1) == _GROUP, "round trip"


def test_state_rejects_tampering_a_wrong_secret_expiry_and_garbage() -> None:
    """Forged, expired and malformed states all verify to None rather than raising."""
    state = sign_state(_GROUP, secret=_SECRET, now=_NOW)
    body, mac = state.split(".")
    forged_body = sign_state("1f6b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d", secret="x", now=_NOW)
    cases = {
        "tampered body": f"{forged_body.split('.')[0]}.{mac}",
        "tampered mac": f"{body}.{mac[:-2]}AA",
        "garbage": "not-a-state",
        "empty": "",
    }
    for name, candidate in cases.items():
        assert verify_state(candidate, secret=_SECRET, now=_NOW) is None, name
    assert verify_state(state, secret="other", now=_NOW) is None, "wrong secret must fail"
    expired = verify_state(state, secret=_SECRET, now=_NOW + STATE_TTL_S + 1)
    assert expired is None, "a state past its TTL must fail"


def test_authorize_url_asks_for_full_control_with_an_account_picker() -> None:
    """The sign-in URL carries every parameter the callback relies on."""
    url = urlparse(
        authorize_url(
            tenant_id="tid",
            client_id="cid",
            redirect_uri="https://daimon.example.com/oauth/teams/files/callback",
            state="s.t",
        )
    )
    assert (url.netloc, url.path) == ("login.microsoftonline.com", "/tid/oauth2/v2.0/authorize")
    assert parse_qs(url.query) == {
        "client_id": ["cid"],
        "response_type": ["code"],
        "redirect_uri": ["https://daimon.example.com/oauth/teams/files/callback"],
        "response_mode": ["query"],
        "scope": ["https://graph.microsoft.com/Sites.FullControl.All"],
        "state": ["s.t"],
        "prompt": ["select_account"],
    }, "authorize URL parameters"


def _graph(requests: list[httpx.Request], *, permission_status: int = 201) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "delegated", "token_type": "Bearer"})
        if request.url.path.endswith("/sites/root"):
            return httpx.Response(
                200,
                json={"id": _SITE_ID, "displayName": "Sales", "webUrl": "https://example.com/s"},
            )
        return httpx.Response(permission_status, json={"id": "perm"})

    return httpx.MockTransport(handler)


async def _grant(transport: httpx.MockTransport) -> str:
    async with httpx.AsyncClient(transport=transport) as http:
        return await grant_team_site(
            http,
            code="the-code",
            tenant_id="tid",
            client_id="cid",
            client_secret=_SECRET,
            redirect_uri="https://daimon.example.com/cb",
            group_id=_GROUP,
        )


async def test_grant_redeems_the_code_finds_the_site_and_grants_write() -> None:
    """Token, root site and permission requests go out in order with the right bodies."""
    requests: list[httpx.Request] = []
    assert await _grant(_graph(requests)) == "Sales", "returns the site's display name"

    token, site, permission = requests
    assert str(token.url) == "https://login.microsoftonline.com/tid/oauth2/v2.0/token"
    assert parse_qs(token.content.decode()) == {
        "grant_type": ["authorization_code"],
        "client_id": ["cid"],
        "client_secret": [_SECRET],
        "code": ["the-code"],
        "redirect_uri": ["https://daimon.example.com/cb"],
        "scope": ["https://graph.microsoft.com/Sites.FullControl.All"],
    }, "token form"
    assert str(site.url) == f"https://graph.microsoft.com/v1.0/groups/{_GROUP}/sites/root"
    assert site.headers["Authorization"] == "Bearer delegated", "site read uses the admin token"
    assert permission.method == "POST" and permission.url.path == (
        f"/v1.0/sites/{_SITE_ID}/permissions"
    ), "permission goes to the site found"
    assert permission.headers["Authorization"] == "Bearer delegated", "grant uses the admin token"
    assert json.loads(permission.content) == {
        "roles": ["write"],
        "grantedToIdentities": [{"application": {"id": "cid", "displayName": "daimon"}}],
    }, "permission body"


async def test_grant_turns_a_forbidden_permission_post_into_an_admin_hint() -> None:
    """A 403 on the grant tells the user to sign in as an admin, with no token in the message."""
    with pytest.raises(SiteGrantFailed) as caught:
        await _grant(_graph([], permission_status=403))
    assert str(caught.value) == "Sign in as a SharePoint or global admin.", "admin hint"
    assert "delegated" not in str(caught.value), "the token never reaches the message"


async def _callback(query: str, granted: list[str]) -> httpx.Response:
    settings = teams_settings(public_url="https://teams.example")
    app = FastAPI()
    async with httpx.AsyncClient() as http:
        app.add_api_route(CALLBACK_PATH, callback_route(settings, http, on_granted=granted.append))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(f"{CALLBACK_PATH}?{query}")


async def test_callback_refuses_a_forged_state() -> None:
    """A state signed with another secret never reaches Microsoft."""
    granted: list[str] = []
    forged = sign_state(_GROUP, secret="other", now=time.time())
    response = await _callback(f"code=c&state={forged}", granted)
    assert response.status_code == 400 and granted == []


async def test_callback_grants_the_signed_team_and_escapes_the_site_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid state hands the code and group over, names the site, and resets the cache."""
    calls: list[dict[str, str]] = []

    async def fake_grant(_http: httpx.AsyncClient, **kwargs: str) -> str:
        calls.append(kwargs)
        return "<Sales>"

    monkeypatch.setattr(site_grant, "grant_team_site", fake_grant)
    granted: list[str] = []
    state = sign_state(_GROUP, secret="test-secret", now=time.time())
    response = await _callback(f"code=the-code&state={state}", granted)

    assert response.status_code == 200, response.text
    assert "&lt;Sales&gt;" in response.text and "<Sales>" not in response.text
    [call] = calls
    assert call["code"] == "the-code" and call["group_id"] == _GROUP
    assert call["redirect_uri"] == f"https://teams.example{CALLBACK_PATH}"
    assert granted == [_GROUP], "the team's channels are rechecked on the next message"


async def test_callback_shows_a_sign_in_error_without_echoing_it() -> None:
    """Entra's `error` redirect is a 400 that repeats nothing from the query."""
    response = await _callback("error=access_denied&error_description=%3Cb%3Enope%3C%2Fb%3E", [])
    assert response.status_code == 400 and "nope" not in response.text
