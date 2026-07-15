"""Pure helpers for Slack OAuth v2 (authorize URL + code exchange + stateless
HMAC signed state). No module-level state; all I/O goes through an injected
``httpx.AsyncClient``. The route handler at the adapter boundary is the catch
site; helpers here let errors propagate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from daimon.core.errors import SlackOAuthError

_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_TOKEN_URL = "https://slack.com/api/oauth.v2.access"

# D-07/D-08: Full v3.0 day-1 bot scope set, hardcoded module-level constant.
# NOT a config field — operators cannot change this without a code change.
SLACK_BOT_SCOPES: tuple[str, ...] = (
    "app_mentions:read",
    "chat:write",
    "commands",
    "users:read",
    "channels:history",
    "groups:history",
    "reactions:write",  # D-01: ⌛ queued-mention reaction parity (Phase 80)
    "files:read",  # attachments & vision: read event.files + fetch url_private
    "channels:read",  # channel tools: public channel metadata + membership checks
    "groups:read",  # channel tools: private channel metadata + membership checks
)

# Full user-token scope set (design D-02): one grant covers hybrid reads,
# DMs, and search — later features never force a per-user re-authorization.
SLACK_USER_SCOPES: tuple[str, ...] = (
    "users:read",  # author display-name resolution (users.info) on user-token reads
    "channels:history",
    "groups:history",
    "channels:read",
    "groups:read",
    "im:history",
    "mpim:history",
    "im:read",
    "mpim:read",
    "search:read",
)


@dataclass(frozen=True)
class SlackExchangeResult:
    """Parsed result of a successful Slack oauth.v2.access exchange."""

    access_token: str | None
    """xoxb- bot token for this workspace. None on user-only (user_scope) exchanges."""
    team_id: str | None
    """Workspace ID (team.id). None for Enterprise Grid installs (D-04)."""
    team_name: str | None
    """Human-readable workspace name (team.name). None for Enterprise Grid."""
    is_enterprise_install: bool
    """True when the install is org-level (Enterprise Grid).

    Must be rejected by the route before any token is stored (SINST-03).
    """
    expires_in: int | None
    """Token lifetime in seconds. Present only when token rotation is enabled on the Slack app."""
    refresh_token: str | None
    """Refresh token. Present only when token rotation is enabled on the Slack app."""
    authed_user_id: str | None = None
    """Slack user id of the authorizing user (authed_user.id)."""
    authed_user_access_token: str | None = None
    """xoxp- user token. Present only when user_scope was requested."""
    authed_user_scope: str | None = None
    """Comma-joined user scopes actually granted."""
    authed_user_expires_in: int | None = None
    """User-token lifetime in seconds (token-rotation apps only)."""
    authed_user_refresh_token: str | None = None
    """User-token refresh token (token-rotation apps only)."""


def build_authorize_url(
    *,
    client_id: str,
    redirect_url: str,
    state: str,
    scopes: tuple[str, ...],
    user_scope: tuple[str, ...] | None = None,
) -> str:
    """Build the Slack OAuth v2 authorize URL.

    Note: Slack authorize uses comma-separated scopes (not space-separated like
    GitHub). ``state`` is a signed HMAC token string produced by ``mint_state``.
    ``user_scope`` requests per-user (xoxp) scopes; empty ``scopes`` omits the
    bot-scope param entirely (user-connect flow re-requests no bot scopes).
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_url,
        "state": state,
    }
    if scopes:
        params["scope"] = ",".join(scopes)
    if user_scope:
        params["user_scope"] = ",".join(user_scope)
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(
    *,
    client: httpx.AsyncClient,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_url: str,
) -> SlackExchangeResult:
    """Exchange an authorization code for a Slack bot token.

    POSTs form-encoded data to oauth.v2.access. On HTTP error, ``raise_for_status``
    propagates. On ``ok:false`` (HTTP 200 with error body), raises ``SlackOAuthError``.
    Errors propagate — the route handler at the adapter boundary is the catch site.
    """
    response = await client.post(
        _TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_url,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    if not payload.get("ok", False):
        raise SlackOAuthError(str(payload.get("error", "unknown_error")))

    # Read is_enterprise_install first to guard team field access (Pitfall 3 + 4).
    is_enterprise_install = bool(payload.get("is_enterprise_install", False))
    team: dict[str, Any] = payload.get("team") or {}
    team_id: str | None = None if is_enterprise_install else team.get("id")
    team_name: str | None = None if is_enterprise_install else team.get("name")

    expires_in_raw = payload.get("expires_in")
    expires_in: int | None = int(expires_in_raw) if expires_in_raw is not None else None

    refresh_token_raw = payload.get("refresh_token")
    refresh_token: str | None = str(refresh_token_raw) if refresh_token_raw is not None else None

    access_token_raw = payload.get("access_token")
    authed_user: dict[str, Any] = payload.get("authed_user") or {}
    authed_expires_raw = authed_user.get("expires_in")

    return SlackExchangeResult(
        access_token=str(access_token_raw) if access_token_raw is not None else None,
        team_id=team_id,
        team_name=team_name,
        is_enterprise_install=is_enterprise_install,
        expires_in=expires_in,
        refresh_token=refresh_token,
        authed_user_id=str(authed_user["id"]) if authed_user.get("id") else None,
        authed_user_access_token=(
            str(authed_user["access_token"]) if authed_user.get("access_token") else None
        ),
        authed_user_scope=str(authed_user["scope"]) if authed_user.get("scope") else None,
        authed_user_expires_in=(
            int(authed_expires_raw) if authed_expires_raw is not None else None
        ),
        authed_user_refresh_token=(
            str(authed_user["refresh_token"]) if authed_user.get("refresh_token") else None
        ),
    )


def mint_state(
    *,
    signing_secret: str,
    now: float,
    payload: Mapping[str, str] | None = None,
) -> str:
    """Mint a stateless HMAC-SHA256 signed OAuth state token (D-05).

    Token format: ``base64url(body).base64url(sig)``. Body is compact JSON
    ``{"nonce": <hex16>, "iat": <int(now)>}`` plus an optional ``"payload"``
    object of string pairs (the user-connect flow binds team + user there).

    ``now`` is injected by the caller — no ``time.time()`` inside the logic.
    """
    nonce = secrets.token_hex(16)
    body: dict[str, Any] = {"nonce": nonce, "iat": int(now)}
    if payload:
        body["payload"] = dict(payload)
    body_bytes = json.dumps(body, separators=(",", ":")).encode()
    sig = hmac.new(signing_secret.encode(), body_bytes, hashlib.sha256).digest()
    return (
        f"{base64.urlsafe_b64encode(body_bytes).decode()}.{base64.urlsafe_b64encode(sig).decode()}"
    )


def verify_state(
    *, token: str, signing_secret: str, now: float, ttl_s: int = 600
) -> dict[str, str] | None:
    """Verify a signed OAuth state token; return its payload or None.

    Returns the minted payload dict (``{}`` when none was minted) when the
    signature is valid and the token is within TTL; None when the signature is
    invalid or the token expired. Raises ValueError on structurally malformed
    tokens (missing '.', non-base64) — the route is the parse-guard catch site.

    No single-use nonce enforcement — replay within TTL is accepted (D-06):
    the callback re-runs idempotent provisioning / upserts, which is harmless.
    """
    parts = token.split(".", 1)
    if len(parts) != 2:
        raise ValueError("malformed state token: missing '.' separator")
    b64_body, b64_sig = parts
    body_bytes = base64.urlsafe_b64decode(b64_body)
    sig = base64.urlsafe_b64decode(b64_sig)
    expected = hmac.new(signing_secret.encode(), body_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    data: dict[str, Any] = json.loads(body_bytes)
    if (now - float(data["iat"])) > ttl_s:
        return None
    raw_payload: dict[str, Any] = data.get("payload") or {}
    return {str(k): str(v) for k, v in raw_payload.items()}


def build_slack_connect_url(
    *,
    app_root_url: str,
    signing_secret: str,
    team_id: str,
    slack_user_id: str,
    now: float,
) -> str:
    """Build the daimon-hosted per-user connect entry URL (design D-04/D-06).

    The signed state binds (team_id, slack_user_id); the callback rejects any
    completion by a different Slack account, so a forwarded link is inert.
    """
    state = mint_state(
        signing_secret=signing_secret,
        now=now,
        payload={"flow": "user_connect", "team_id": team_id, "slack_user_id": slack_user_id},
    )
    return f"{app_root_url}/oauth/slack/connect?{urlencode({'state': state})}"
