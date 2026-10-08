"""Tests for the Teams site grant: signed state, the sign-in URL, and the Graph calls."""

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
    GrantedSite,
    GrantTarget,
    SiteGrantFailed,
    authorize_url,
    callback_route,
    grant_channel_site,
    sign_state,
    verify_state,
)
from daimon.core.teams_sharepoint import DriveFolder
from fastapi import FastAPI

from .conftest import teams_settings

_GROUP = "0f6b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d"
_SECRET = "client-secret"
_NOW = 1_800_000_000.0
_SITE_ID = "example.sharepoint.com,1111,2222"
_CHANNEL = "19:private-1@thread.tacv2"
_TARGET = GrantTarget(_GROUP, _CHANNEL)
_FOLDER_URL = "https://example.sharepoint.com/sites/Sales-Private/Shared%20Documents/Private"


def test_state_round_trips_the_group_and_channel() -> None:
    """A fresh state verifies back to the channel it was signed for, colons and all."""
    state = sign_state(_TARGET, secret=_SECRET, now=_NOW)
    assert verify_state(state, secret=_SECRET, now=_NOW + 1) == _TARGET, "round trip"


def test_state_rejects_tampering_a_wrong_secret_expiry_and_garbage() -> None:
    """Forged, expired and malformed states all verify to None rather than raising."""
    state = sign_state(_TARGET, secret=_SECRET, now=_NOW)
    body, mac = state.split(".")
    other = GrantTarget("1f6b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d", _CHANNEL)
    forged_body = sign_state(other, secret="x", now=_NOW)
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
        "scope": [
            "https://graph.microsoft.com/Sites.FullControl.All "
            "https://graph.microsoft.com/Files.Read.All"
        ],
        "state": ["s.t"],
        "prompt": ["select_account"],
    }, "authorize URL parameters"


def _graph(
    requests: list[httpx.Request], *, permission_status: int = 201, folder_url: str = _FOLDER_URL
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "delegated", "token_type": "Bearer"})
        if request.url.path.endswith("/filesFolder"):
            folder = {
                "id": "01PRIVATE",
                "webUrl": folder_url,
                "parentReference": {"driveId": "b!p"},
            }
            return httpx.Response(200, json=folder)
        if request.method == "GET":
            site = {"id": _SITE_ID, "displayName": "Sales - Private", "webUrl": "https://x.example"}
            return httpx.Response(200, json=site)
        return httpx.Response(permission_status, json={"id": "perm"})

    return httpx.MockTransport(handler)


async def _grant(transport: httpx.MockTransport) -> GrantedSite:
    async with httpx.AsyncClient(transport=transport) as http:
        return await grant_channel_site(
            http,
            code="the-code",
            tenant_id="tid",
            client_id="cid",
            client_secret=_SECRET,
            redirect_uri="https://daimon.example.com/cb",
            target=_TARGET,
        )


async def test_grant_finds_the_channels_folder_and_grants_write_on_the_site_holding_it() -> None:
    """Token, the channel's filesFolder, its site, then the permission, all as the admin."""
    requests: list[httpx.Request] = []
    granted = await _grant(_graph(requests))

    assert granted == GrantedSite(
        name="Sales - Private",
        site_id=_SITE_ID,
        folder=DriveFolder(drive_id="b!p", item_id="01PRIVATE"),
    ), "the site by name, and the folder to store"
    token, folder, site, permission = requests
    assert str(token.url) == "https://login.microsoftonline.com/tid/oauth2/v2.0/token"
    assert parse_qs(token.content.decode()) == {
        "grant_type": ["authorization_code"],
        "client_id": ["cid"],
        "client_secret": [_SECRET],
        "code": ["the-code"],
        "redirect_uri": ["https://daimon.example.com/cb"],
        "scope": [
            "https://graph.microsoft.com/Sites.FullControl.All "
            "https://graph.microsoft.com/Files.Read.All"
        ],
    }, "token form"
    assert folder.url.path == f"/v1.0/teams/{_GROUP}/channels/{_CHANNEL}/filesFolder"
    assert site.url.path == "/v1.0/sites/example.sharepoint.com:/sites/Sales-Private", (
        "the channel's own site, from its folder's URL"
    )
    assert permission.method == "POST" and permission.url.path == (
        f"/v1.0/sites/{_SITE_ID}/permissions"
    ), "permission goes to the site found"
    assert all(r.headers["Authorization"] == "Bearer delegated" for r in (folder, site, permission))
    assert json.loads(permission.content) == {
        "roles": ["write"],
        "grantedToIdentities": [{"application": {"id": "cid", "displayName": "daimon"}}],
    }, "permission body"


async def test_grant_turns_a_forbidden_permission_post_into_an_admin_hint() -> None:
    """A 403 on the grant tells the user to sign in as an admin, with no token in the message."""
    with pytest.raises(SiteGrantFailed) as caught:
        await _grant(_graph([], permission_status=403))
    assert str(caught.value) == (
        "Sign in as a SharePoint or global admin who is a member of this channel."
    ), "admin hint"
    assert "delegated" not in str(caught.value), "the token never reaches the message"


@pytest.mark.parametrize(
    "folder_url",
    [
        "https://evil.example/sites/x/Shared%20Documents/General",
        "https://example.sharepoint.com/personal/u/Documents",
        "https://example.sharepoint.com/sites/../Shared%20Documents",
        "https://example.sharepoint.com/sites",
        "https://example.sharepoint.com/sites//Shared%20Documents",
    ],
)
async def test_grant_refuses_a_folder_off_a_sharepoint_site_before_any_grant(
    folder_url: str,
) -> None:
    """The folder's URL picks the site to grant: anything but a SharePoint site stops there."""
    requests: list[httpx.Request] = []
    with pytest.raises(SiteGrantFailed):
        await _grant(_graph(requests, folder_url=folder_url))
    assert [r.method for r in requests] == ["POST", "GET"], "token and folder only, no grant"


@pytest.mark.parametrize(
    ("folder_url", "site_path"),
    [
        ("https://example.sharepoint.com/teams/Sales/Shared%20Documents/General", "teams/Sales"),
        (
            "https://example.sharepoint.com/sites/Sales%20Q3-Private/Shared%20Documents",
            "sites/Sales%20Q3-Private",
        ),
    ],
)
async def test_grant_looks_up_the_site_by_its_managed_path_and_encoded_name(
    folder_url: str, site_path: str
) -> None:
    """Either managed path, and a name with a space, reach Graph as the folder's URL had them."""
    requests: list[httpx.Request] = []
    await _grant(_graph(requests, folder_url=folder_url))
    site = requests[2]
    assert site.url.raw_path.decode() == f"/v1.0/sites/example.sharepoint.com:/{site_path}"


async def _callback(query: str, granted: list[tuple[GrantTarget, GrantedSite]]) -> httpx.Response:
    settings = teams_settings(public_url="https://teams.example")
    app = FastAPI()

    async def on_granted(target: GrantTarget, site: GrantedSite) -> None:
        granted.append((target, site))

    async with httpx.AsyncClient() as http:
        app.add_api_route(CALLBACK_PATH, callback_route(settings, http, on_granted=on_granted))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(f"{CALLBACK_PATH}?{query}")


async def test_callback_refuses_a_forged_state() -> None:
    """A state signed with another secret never reaches Microsoft."""
    granted: list[tuple[GrantTarget, GrantedSite]] = []
    forged = sign_state(_TARGET, secret="other", now=time.time())
    response = await _callback(f"code=c&state={forged}", granted)
    assert response.status_code == 400 and granted == []


async def test_callback_grants_the_signed_channel_and_escapes_the_site_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid state hands the code and channel over, names the site, and stores the grant."""
    calls: list[dict[str, object]] = []
    site = GrantedSite("<Sales>", _SITE_ID, DriveFolder(drive_id="b!p", item_id="01PRIVATE"))

    async def fake_grant(_http: httpx.AsyncClient, **kwargs: object) -> GrantedSite:
        calls.append(kwargs)
        return site

    monkeypatch.setattr(site_grant, "grant_channel_site", fake_grant)
    granted: list[tuple[GrantTarget, GrantedSite]] = []
    state = sign_state(_TARGET, secret="test-secret", now=time.time())
    response = await _callback(f"code=the-code&state={state}", granted)

    assert response.status_code == 200, response.text
    assert "&lt;Sales&gt;" in response.text and "<Sales>" not in response.text
    [call] = calls
    assert call["code"] == "the-code" and call["target"] == _TARGET
    assert call["redirect_uri"] == f"https://teams.example{CALLBACK_PATH}"
    assert granted == [(_TARGET, site)], "the folder is stored and the channel rechecked"


async def test_callback_shows_a_sign_in_error_without_echoing_it() -> None:
    """Entra's `error` redirect is a 400 that repeats nothing from the query."""
    response = await _callback("error=access_denied&error_description=%3Cb%3Enope%3C%2Fb%3E", [])
    assert response.status_code == 400 and "nope" not in response.text
