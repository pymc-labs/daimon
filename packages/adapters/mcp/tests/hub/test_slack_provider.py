"""Slack login: user_scope on authorize, user token lifted from authed_user, tenant map baked in."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from daimon.adapters.mcp.hub.slack_provider import SlackHubProvider, SlackTokenVerifier
from daimon.core.slack_oauth import SLACK_USER_SCOPES
from daimon.testing.factories import make_tenant
from key_value.aio.stores.memory import MemoryStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

_EXCHANGE_OK = {
    "ok": True,
    "team": {"id": "T1", "name": "Acme"},
    "authed_user": {"id": "U1", "access_token": "xoxp-user", "scope": "channels:read"},
}


def _slack_http(
    exchange: dict[str, object], *, auth_test: dict[str, object] | None = None
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth.v2.access":
            return httpx.Response(200, json=exchange)
        if request.url.path == "/api/auth.test":
            return httpx.Response(
                200, json=auth_test or {"ok": True, "user_id": "U1", "team_id": "T1"}
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.com")


def _provider(
    session_factory: async_sessionmaker[AsyncSession], http: httpx.AsyncClient
) -> SlackHubProvider:
    return SlackHubProvider(
        client_id="cid",
        client_secret="csecret",
        base_url="https://example.test/slack",
        session_factory=session_factory,
        client_storage=MemoryStore(),
        jwt_signing_key=b"0" * 32,
        http_client=http,
    )


async def test_authorize_url_uses_user_scope_not_scope(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider(db_session_factory, _slack_http(_EXCHANGE_OK))
    url = provider._build_upstream_authorize_url(
        "txn", {"scopes": [], "client_redirect_uri": "http://localhost/cb"}
    )  # pyright: ignore[reportPrivateUsage]
    qs = parse_qs(urlparse(url).query)
    assert "scope" not in qs, f"bot scope must not be requested, got {qs!r}"
    assert qs["user_scope"] == [",".join(SLACK_USER_SCOPES)], f"got {qs.get('user_scope')!r}"
    assert qs["redirect_uri"] == ["https://example.test/slack/auth/callback"], (
        f"got {qs.get('redirect_uri')!r}"
    )


async def test_token_client_lifts_user_token_to_top_level(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider(db_session_factory, _slack_http(_EXCHANGE_OK))
    client = provider._create_upstream_oauth_client()  # pyright: ignore[reportPrivateUsage]
    tokens = await client.fetch_token(
        url="https://slack.com/api/oauth.v2.access", code="c", redirect_uri="r"
    )
    assert tokens["access_token"] == "xoxp-user", f"got {tokens!r}"
    assert tokens["team"] == {"id": "T1", "name": "Acme"} and tokens["authed_user"]["id"] == "U1"
    assert "expires_in" not in tokens, "non-rotating slack tokens must not claim an expiry"


async def test_token_client_rejects_enterprise_install(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider(
        db_session_factory, _slack_http({**_EXCHANGE_OK, "is_enterprise_install": True})
    )
    client = provider._create_upstream_oauth_client()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ValueError, match="Enterprise Grid"):
        await client.fetch_token(
            url="https://slack.com/api/oauth.v2.access", code="c", redirect_uri="r"
        )


async def test_extract_upstream_claims_bakes_single_tenant(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()
    provider = _provider(db_session_factory, _slack_http(_EXCHANGE_OK))
    claims = await provider._extract_upstream_claims(  # pyright: ignore[reportPrivateUsage]
        {
            "access_token": "xoxp-user",
            "team": {"id": "T1", "name": "Acme"},
            "authed_user": {"id": "U1"},
        }
    )
    assert claims is not None and [t["tenant_id"] for t in claims["tenants"]] == [str(tenant.id)], (
        f"got {claims!r}"
    )
    assert claims["tenants"][0]["workspace_name"] == "Acme" and claims["platform_user_id"] == "U1"


async def test_verifier_accepts_live_token_and_rejects_dead_one() -> None:
    live = SlackTokenVerifier(http_client=_slack_http(_EXCHANGE_OK))
    token = await live.verify_token("xoxp-user")
    assert token is not None and token.claims["sub"] == "U1", f"got {token!r}"

    dead = SlackTokenVerifier(
        http_client=_slack_http(_EXCHANGE_OK, auth_test={"ok": False, "error": "invalid_auth"})
    )
    assert await dead.verify_token("xoxp-user") is None, "revoked token must fail verification"


async def test_cookie_name_is_platform_specific(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider(db_session_factory, _slack_http(_EXCHANGE_OK))
    assert provider._cookie_name("MCP_CONSENT_BINDING").endswith("MCP_CONSENT_BINDING_SLACK")  # pyright: ignore[reportPrivateUsage]
