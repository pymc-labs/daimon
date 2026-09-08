"""Slack login for the /slack/mcp hub mount.

FastMCP has no Slack provider, and Slack's OAuth differs from the generic
proxy in three ways this module absorbs:

- The authorize endpoint takes ``user_scope`` for a user (xoxp) grant;
  ``scope`` would request bot scopes and install the app again.
- ``oauth.v2.access`` returns the user token under ``authed_user.access_token``.
  The proxy reads ``access_token`` at the top level, so the token client here
  performs the exchange itself and lifts the user token up.
- Slack does not support PKCE, so it is not forwarded upstream.

``_extract_upstream_claims`` runs once per exchange with the lifted response,
which already carries ``team.id``, ``team.name`` and ``authed_user.id``; no
second Slack call is needed to build the tenant map. The verifier runs
``auth.test`` per request so a token revoked upstream stops working
immediately rather than at the next refresh (non-rotating Slack tokens never
refresh).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.core.hub_identity import resolve_hub_tenants
from daimon.core.slack_oauth import SLACK_USER_SCOPES
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from key_value.aio.protocols import AsyncKeyValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"
# Non-rotating xoxp tokens carry no expiry; cap the issued login at 30 days.
_FALLBACK_EXPIRY_S = 30 * 24 * 60 * 60


class SlackTokenVerifier(TokenVerifier):
    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        super().__init__()
        self._http = http_client or httpx.AsyncClient(timeout=10.0)

    async def verify_token(self, token: str) -> AccessToken | None:
        response = await self._http.post(
            SLACK_AUTH_TEST_URL, headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code != 200:
            return None
        body = response.json()
        if not body.get("ok"):
            return None
        return AccessToken(
            token=token,
            client_id="slack",
            scopes=[],
            expires_at=None,
            claims={"sub": str(body["user_id"]), "team_id": str(body["team_id"])},
        )


class _SlackTokenClient:
    """Stand-in for authlib's client: only ``fetch_token`` is ever called on the exchange path."""

    def __init__(self, *, http: httpx.AsyncClient, client_id: str, client_secret: str) -> None:
        self._http = http
        self.client_id = client_id
        self.client_secret = client_secret

    async def fetch_token(
        self, *, url: str, code: str, redirect_uri: str, **_: Any
    ) -> dict[str, Any]:
        response = await self._http.post(
            url,
            data={
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri,
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if not payload.get("ok"):
            raise ValueError(f"slack exchange failed: {payload.get('error', 'unknown_error')}")
        if payload.get("is_enterprise_install"):
            raise ValueError("Enterprise Grid installs are not supported")
        authed_user: dict[str, Any] = payload.get("authed_user") or {}
        if "access_token" not in authed_user:
            raise ValueError("slack exchange returned no user token")
        lifted: dict[str, Any] = {
            "access_token": authed_user["access_token"],
            "token_type": "bearer",
            "scope": authed_user.get("scope", ""),
            "team": payload.get("team") or {},
            "authed_user": {"id": authed_user.get("id")},
        }
        if authed_user.get("expires_in") is not None:
            lifted["expires_in"] = int(authed_user["expires_in"])
        if authed_user.get("refresh_token"):
            lifted["refresh_token"] = authed_user["refresh_token"]
        return lifted


class SlackHubProvider(OAuthProxy):
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        client_storage: AsyncKeyValue,
        jwt_signing_key: bytes,
        allowed_client_redirect_uris: list[str],
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=10.0)
        super().__init__(
            upstream_authorization_endpoint=SLACK_AUTHORIZE_URL,
            upstream_token_endpoint=SLACK_TOKEN_URL,
            upstream_client_id=client_id,
            upstream_client_secret=client_secret,
            token_verifier=SlackTokenVerifier(http_client=self._http),
            base_url=base_url,
            forward_pkce=False,
            client_storage=client_storage,
            jwt_signing_key=jwt_signing_key,
            allowed_client_redirect_uris=allowed_client_redirect_uris,
            fallback_access_token_expiry_seconds=_FALLBACK_EXPIRY_S,
        )
        self._session_factory = session_factory

    def _build_upstream_authorize_url(self, txn_id: str, transaction: dict[str, Any]) -> str:
        url = super()._build_upstream_authorize_url(txn_id, transaction)
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "scope"]
        query.append(("user_scope", ",".join(SLACK_USER_SCOPES)))
        return urlunsplit(parts._replace(query=urlencode(query)))

    def _create_upstream_oauth_client(self) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        secret = self._upstream_client_secret
        return _SlackTokenClient(
            http=self._http,
            client_id=self._upstream_client_id,
            client_secret=secret.get_secret_value() if secret is not None else "",
        )

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        team: dict[str, Any] = idp_tokens.get("team") or {}
        authed_user: dict[str, Any] = idp_tokens.get("authed_user") or {}
        team_id = str(team["id"])
        user_id = str(authed_user["id"])
        tenants = await resolve_hub_tenants(
            self._session_factory,
            platform="slack",
            platform_user_id=user_id,
            workspaces=[(team_id, str(team.get("name") or team_id))],
        )
        return encode_hub_claims(platform="slack", platform_user_id=user_id, tenants=tenants)

    def _cookie_name(self, base_name: str) -> str:
        return super()._cookie_name(f"{base_name}_SLACK")
