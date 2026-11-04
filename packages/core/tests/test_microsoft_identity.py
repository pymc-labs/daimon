"""Entra v2 helpers for Connect Microsoft: endpoints, token requests and the identity check."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from daimon.core.config import MicrosoftAccountsSettings
from daimon.core.microsoft_identity import (
    MicrosoftGrantRevoked,
    MicrosoftIdentityError,
    MicrosoftIdentityMismatch,
    MicrosoftTokens,
    build_authorize_url,
    exchange_code,
    refresh_tokens,
    verify_identity,
)
from pydantic import HttpUrl, SecretStr

TENANT = "11111111-1111-1111-1111-111111111111"
OID = "22222222-2222-2222-2222-222222222222"
CONFIG = MicrosoftAccountsSettings(
    client_id="app-1",
    client_secret=SecretStr("app-secret"),
    tenant_id=TENANT.upper(),
    authority=HttpUrl("https://login.example.com"),
)


def _id_token(**claims: object) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def _tokens(id_token: str | None) -> MicrosoftTokens:
    return MicrosoftTokens(
        access_token="at", refresh_token="rt", expires_in=3600, scope="", id_token=id_token
    )


def test_settings_canonicalise_the_tenant_and_report_configured() -> None:
    assert CONFIG.tenant_id == TENANT, "tenant id must be stored as a lowercase canonical GUID"
    assert CONFIG.configured, "client id, secret and tenant together configure the feature"
    assert not MicrosoftAccountsSettings().configured, "the feature is inert when unset"


def test_authorize_url_asks_to_pick_an_account_unless_told_not_to() -> None:
    def prompt(*, select_account: bool) -> list[str] | None:
        url = build_authorize_url(
            CONFIG,
            redirect_uri="https://d.example/cb",
            state="st",
            code_challenge="ch",
            select_account=select_account,
        )
        return parse_qs(urlsplit(url).query).get("prompt")

    assert prompt(select_account=True) == ["select_account"], "the account picker is shown"
    assert prompt(select_account=False) is None, "sign-in leaves the account to the browser"


def test_authorize_url_uses_the_tenant_endpoint_with_pkce() -> None:
    url = urlsplit(
        build_authorize_url(
            CONFIG, redirect_uri="https://d.example/cb", state="st", code_challenge="ch"
        )
    )
    query = parse_qs(url.query)
    assert (url.netloc, url.path) == ("login.example.com", f"/{TENANT}/oauth2/v2.0/authorize"), (
        "sign-in must go to the configured tenant's v2 authorize endpoint, not /common"
    )
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"] == ["ch"], (
        "PKCE must be S256"
    )
    assert {"openid", "offline_access", "Mail.Send"} <= set(query["scope"][0].split()), (
        "the grant must carry openid for the id_token and offline_access for refresh"
    )
    assert query["state"] == ["st"], "state must round-trip to the callback"


async def test_exchange_posts_secret_and_verifier_to_the_tenant_token_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tokens = await exchange_code(
            client, CONFIG, code="c", code_verifier="v", redirect_uri="https://d.example/cb"
        )
    form = parse_qs(seen[0].content.decode())
    assert seen[0].url.path == f"/{TENANT}/oauth2/v2.0/token", (
        "the exchange must hit the tenant token endpoint"
    )
    assert form["client_secret"] == ["app-secret"] and form["code_verifier"] == ["v"], (
        "a confidential client sends client_secret_post plus the PKCE verifier"
    )
    assert (tokens.access_token, tokens.refresh_token) == ("at", "rt"), "tokens must be parsed"


@pytest.mark.parametrize("error", ["invalid_grant", "interaction_required"])
async def test_refresh_reports_a_revoked_grant(error: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": error, "error_description": "AADSTS details"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MicrosoftGrantRevoked) as caught:
            await refresh_tokens(client, CONFIG, refresh_token="rt")
    assert "AADSTS" not in str(caught.value), (
        "only the error code may surface, never the description"
    )


async def test_refresh_keeps_other_failures_distinct_from_revocation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MicrosoftIdentityError) as caught:
            await refresh_tokens(client, CONFIG, refresh_token="rt")
    assert not isinstance(caught.value, MicrosoftGrantRevoked), (
        "an outage must not be mistaken for a revoked grant, which would delete the connection"
    )


def test_verify_identity_accepts_the_teams_person() -> None:
    token = _id_token(tid=TENANT, aud="app-1", oid=OID.upper())
    assert verify_identity(_tokens(token), CONFIG, expected_user_id=OID) == OID, (
        "the matching account in the configured tenant must be accepted, case-insensitively"
    )


_REFUSED = {
    "another tenant": _id_token(tid="33333333-3333-3333-3333-333333333333", aud="app-1", oid=OID),
    "another person": _id_token(
        tid=TENANT, aud="app-1", oid="44444444-4444-4444-4444-444444444444"
    ),
    "another app": _id_token(tid=TENANT, aud="other-app", oid=OID),
    "no oid": _id_token(tid=TENANT, aud="app-1"),
    "malformed": "not-a-jwt",
    "no id_token": None,
}


@pytest.mark.parametrize("id_token", list(_REFUSED.values()), ids=list(_REFUSED))
def test_verify_identity_refuses(id_token: str | None) -> None:
    with pytest.raises(MicrosoftIdentityMismatch):
        verify_identity(_tokens(id_token), CONFIG, expected_user_id=OID)
