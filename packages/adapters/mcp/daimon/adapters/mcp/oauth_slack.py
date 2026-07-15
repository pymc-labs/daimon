"""OAuth routes and branded HTML pages for the Slack install flow.

Mounted on the FastMCP-derived ASGI app via `add_route` in server.py — siblings
of /healthz/readyz and /oauth/github/*. They intentionally bypass
IdentityMiddleware because the HMAC-signed state validation IS the auth.

Catch at boundaries only: this module IS the catch boundary. Route
handlers catch narrowly at their edges.
Presentation helpers in this module are pure — no I/O, no exceptions swallowed.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import aiohttp
import httpx
import structlog
from cryptography.fernet import MultiFernet
from daimon.core.config import Settings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import SlackOAuthError
from daimon.core.github_credentials import encrypt_token
from daimon.core.observability import capture_exception_with_scope
from daimon.core.slack_oauth import (
    SLACK_BOT_SCOPES,
    SLACK_USER_SCOPES,
    build_authorize_url,
    exchange_code,
    mint_state,
    verify_state,
)
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.slack_user_tokens import upsert_slack_user_token
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = structlog.get_logger(__name__)

RouteHandler = Callable[[Request], Awaitable[Response]]
HttpxClientFactory = Callable[[], httpx.AsyncClient]

# ---------------------------------------------------------------------------
# Shared branded template helper
# ---------------------------------------------------------------------------

_FONTS_PRECONNECT = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Bricolage+Grotesque:wght@400;600&amp;"
    "family=Hanken+Grotesk:wght@400;600&amp;"
    "family=Spline+Sans+Mono:wght@400&amp;"
    'display=swap">'
)

_CSS = """
:root {
  --stage: #0b110e;
  --stage-raised: #141d19;
  --accent: #44a171;
  --accent-press: #2f8e60;
  --text: #ebf0ed;
  --text-dim: #9fa7a3;
  --border: #28302c;
  --slate: #43586a;
  --rose: #c68d8c;
  --radius-pill: 999px;
  --radius-card: 16px;
  --shadow-glow: 0 0 12px var(--accent);
  --shadow-card: 0 8px 28px -8px #000502b3;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--stage);
  color: var(--text);
  font-family: "Hanken Grotesk", system-ui, sans-serif;
  font-size: 16px;
  line-height: 1.5;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 64px 16px;
}

.card {
  background: var(--stage-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  max-width: 480px;
  width: 100%;
  overflow: hidden;
}

.status-bar {
  height: 4px;
  background: var(--accent);
}

.status-bar--rose {
  background: var(--rose);
}

.card-body {
  padding: 32px;
}

h1 {
  font-family: "Bricolage Grotesque", system-ui, sans-serif;
  font-size: 32px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--text);
  margin-bottom: 8px;
}

h2 {
  font-family: "Bricolage Grotesque", system-ui, sans-serif;
  font-size: 20px;
  font-weight: 600;
  line-height: 1.3;
  color: var(--text);
  margin-top: 24px;
  margin-bottom: 12px;
}

.subtitle {
  color: var(--text-dim);
  margin-bottom: 16px;
}

p {
  color: var(--text);
  margin-bottom: 16px;
}

p.dim {
  color: var(--text-dim);
}

.scope-list {
  list-style: none;
  margin: 0 0 24px 0;
  padding: 0;
}

.scope-list li {
  display: flex;
  align-items: baseline;
  gap: 4px;
  margin-bottom: 12px;
  color: var(--text);
}

.scope-list li code {
  color: var(--text-dim);
}

code {
  font-family: "Spline Sans Mono", ui-monospace, monospace;
  font-size: 14px;
  line-height: 1.5;
  background: var(--stage);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
}

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  padding: 0 24px;
  background: var(--accent);
  color: var(--stage);
  font-family: "Hanken Grotesk", system-ui, sans-serif;
  font-size: 16px;
  font-weight: 600;
  text-decoration: none;
  border-radius: var(--radius-pill);
  box-shadow: var(--shadow-glow);
  margin-top: 24px;
  margin-bottom: 24px;
  transition: background 0.15s ease;
}

.btn:hover {
  background: var(--accent-press);
}

.btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.btn--full {
  width: 100%;
}

.footer {
  color: var(--text-dim);
  font-size: 16px;
  margin-top: 24px;
  margin-bottom: 0;
}

.emphasis {
  font-weight: 600;
  color: var(--text);
}

@media (max-width: 520px) {
  body {
    padding: 0;
    justify-content: flex-start;
  }

  .card {
    border-radius: 0;
    max-width: 100%;
    min-height: 100vh;
  }

  .card-body {
    padding: 24px;
  }

  .btn {
    width: 100%;
  }
}

@media (prefers-reduced-motion: no-preference) {
  .btn {
    box-shadow: var(--shadow-glow);
  }
}
"""


def _page(
    *,
    title: str,
    state_bar: str,
    body_html: str,
    status: int = 200,
) -> HTMLResponse:
    """Render a complete branded daimon HTML document.

    Args:
        title: The <title> text for this page.
        state_bar: CSS class suffix for the top status stripe —
            "" for accent (success/install), "--rose" for rejection/error.
        body_html: Pre-escaped inner HTML for the card body.
        status: HTTP status code (default 200).
    """
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title, quote=False)}</title>
{_FONTS_PRECONNECT}
<style>{_CSS}</style>
</head>
<body>
<div class="card">
  <div class="status-bar{state_bar}"></div>
  <div class="card-body">
    {body_html}
  </div>
</div>
</body>
</html>"""
    return HTMLResponse(doc, status_code=status)


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------

_SCOPES_HTML = """
<ul class="scope-list">
  <li>see when you @mention daimon <code>app_mentions:read</code></li>
  <li>post replies and status updates <code>chat:write</code></li>
  <li>run daimon&#39;s slash commands (e.g. <code>/agent-setup</code>) <code>commands</code></li>
  <li>check who&#39;s an admin before admin actions <code>users:read</code></li>
  <li>read recent messages in public channels it&#39;s added to <code>channels:history</code></li>
  <li>read recent messages in private channels it&#39;s added to <code>groups:history</code></li>
  <li>see which channels exist and who&#39;s in them, to enforce your channel permissions
  <code>channels:read</code> <code>groups:read</code></li>
</ul>
"""


def _format_credit(amount: Decimal) -> str:
    """Format a signup-credit Decimal as display copy (e.g. "$5", "$7.50").

    The amount is an operator-configured server-side value, not user input,
    so interpolating it does not violate the static-copy discipline that
    `_error_html` enforces for attacker-influenceable strings.
    """
    if amount == amount.to_integral_value():
        return f"${int(amount)}"
    return f"${amount:.2f}"


def _install_landing_html(
    *,
    authorize_url: str,
    signup_credit: Decimal,
) -> HTMLResponse:
    """Branded install landing page (Page 1 per UI-SPEC).

    Shows the 6 locked SLACK_BOT_SCOPES in plain language before the user
    clicks through to Slack's consent screen.
    """
    safe_href = html.escape(authorize_url, quote=True)
    credit = _format_credit(signup_credit)
    body = f"""
<h1>your server just hired a data scientist</h1>
<p class="subtitle">· by daimon</p>
<p>add daimon to your Slack workspace and get {credit} of credit on us — data
analysis, code review, and research, all from Slack.</p>
<h2>what daimon will be able to do</h2>
{_SCOPES_HTML}
<a class="btn" href="{safe_href}">Add to Slack</a>
<p class="footer">daimon reads channels you invite it to — and, for members who
connect their account, whatever they can already see.</p>
"""
    return _page(title="add daimon to Slack", state_bar="", body_html=body)


def _success_html(
    *,
    workspace: str,
    signup_credit: Decimal,
) -> HTMLResponse:
    """Branded success page (Page 2 per UI-SPEC).

    `workspace` is the `team.name` from the Slack OAuth response —
    attacker-influenceable, so it is HTML-escaped before interpolation.
    """
    safe = html.escape(workspace, quote=False)
    credit = _format_credit(signup_credit)
    body = f"""
<h1>installed in {safe}</h1>
<p>next: @mention <code>@daimon</code> in any channel, or run
<code>/agent-setup</code> to get started.</p>
<p>you get {credit} of credit on us.</p>
<p class="dim">you can close this tab and head back to Slack.</p>
"""
    return _page(title="daimon installed", state_bar="", body_html=body)


def _enterprise_rejection_html() -> HTMLResponse:
    """Branded Enterprise-Grid rejection page (Page 3 per UI-SPEC).

    Called when `is_enterprise_install=true` in the OAuth response.
    No token was persisted, no tenant was provisioned.
    """
    body = """
<h1>org-level installs aren&#39;t supported yet</h1>
<p>daimon installs per workspace, not at the Enterprise Grid org level.</p>
<p><span class="emphasis">we didn&#39;t save anything &mdash;
no token, no data was stored.</span></p>
<p>ask a workspace admin to add daimon directly inside their workspace.</p>
"""
    return _page(
        title="daimon — workspace install required",
        state_bar=" status-bar--rose",
        body_html=body,
        status=200,
    )


def _connected_html() -> HTMLResponse:
    """Branded user-connect success page. Static copy only."""
    body = """
<h1>connected</h1>
<p>daimon can now read Slack as you — any channel or DM you can see, plus search.</p>
<p class="dim">go back to Slack and re-ask. you can disconnect any time via
<code>/privacy</code>.</p>
"""
    return _page(title="daimon — Slack account connected", state_bar="", body_html=body)


_ERROR_COPY: dict[str, tuple[str, str, int]] = {
    "expired": (
        "this install link expired",
        "start over from the install page and try again.",
        400,
    ),
    "unconfigured": (
        "daimon's Slack install isn't set up here",
        "the operator needs to finish configuring daimon's Slack app.",
        500,
    ),
    "exchange_failed": (
        "Slack couldn't complete the install",
        "something went wrong on Slack's side — please try installing again.",
        502,
    ),
    "wrong_account": (
        "this connect link was for a different Slack user",
        "open daimon's connect link from your own Slack account and try again. nothing was saved.",
        400,
    ),
}


def _error_html(
    *,
    kind: Literal["expired", "unconfigured", "exchange_failed", "wrong_account"],
) -> HTMLResponse:
    """Branded error variant (Page 4 per UI-SPEC).

    STATIC copy only — never interpolates exception text, request params, or
    any external value. Mirror of `oauth_github.py` static-string discipline.
    """
    headline, body_text, status = _ERROR_COPY[kind]
    body = f"""
<h1>{html.escape(headline, quote=False)}</h1>
<p>{html.escape(body_text, quote=False)}</p>
"""
    return _page(
        title="daimon — install error",
        state_bar=" status-bar--rose",
        body_html=body,
        status=status,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

# Connect links ride in nudges / tool errors and are clicked minutes later —
# 1 h entry TTL; callback allows the extra Slack round-trip on top.
_CONNECT_STATE_TTL_S = 3600
# Applies to ALL callback verifies, including install-flow states minted with
# a 600s window — the callback intentionally does not re-enforce that
# tighter TTL. This is fine by design (D-06): callback verification is
# replay-idempotent, so a wider TTL here doesn't let a stale install state do
# anything a fresh one couldn't.
_CALLBACK_STATE_TTL_S = 3900


class _SlackConfig:
    """Internal: validated Slack OAuth config ready for the route handlers."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        signing_secret: str,
        redirect_url: str,
        fernet: MultiFernet,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.signing_secret = signing_secret
        self.redirect_url = redirect_url
        self.fernet = fernet


def _resolve_config_or_error(
    settings: Settings,
    fernet: MultiFernet | None,
) -> _SlackConfig | Response:
    """Return a validated _SlackConfig or a 500 error Response when unconfigured.

    Guards: slack settings absent, client_id/client_secret missing, fernet
    absent, or app_root_url unset. Any missing component → "unconfigured" 500.
    """
    s = settings.slack
    if s is None or s.client_id is None or s.client_secret is None:
        return _error_html(kind="unconfigured")
    if fernet is None:
        return _error_html(kind="unconfigured")
    root_url = settings.mcp.app_root_url
    if root_url is None:
        return _error_html(kind="unconfigured")
    return _SlackConfig(
        client_id=s.client_id,
        client_secret=s.client_secret.get_secret_value(),
        signing_secret=s.signing_secret.get_secret_value(),
        # Pitfall 6: derived ONCE, used identically in authorize + exchange.
        redirect_url=root_url + "/oauth/slack/callback",
        fernet=fernet,
    )


def build_oauth_slack_routes(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    fernet: MultiFernet | None,
    client_factory: HttpxClientFactory = lambda: httpx.AsyncClient(timeout=10.0),
) -> tuple[RouteHandler, RouteHandler, RouteHandler]:
    """Wire the Slack install + callback + connect handlers with their dependencies.

    Returns (install_handler, callback_handler, connect_handler) to be mounted
    by server.py. All handlers are the catch boundary for Slack OAuth errors.
    """

    async def install_handler(request: Request) -> Response:
        cfg = _resolve_config_or_error(settings, fernet)
        if isinstance(cfg, Response):
            return cfg
        state = mint_state(signing_secret=cfg.signing_secret, now=time.time())
        authorize_url = build_authorize_url(
            client_id=cfg.client_id,
            redirect_url=cfg.redirect_url,
            state=state,
            scopes=SLACK_BOT_SCOPES,
        )
        return _install_landing_html(
            authorize_url=authorize_url,
            signup_credit=settings.billing.signup_credit,
        )

    async def callback_handler(request: Request) -> Response:
        cfg = _resolve_config_or_error(settings, fernet)
        if isinstance(cfg, Response):
            return cfg

        raw_state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")

        # T-79-01: verify HMAC-signed state before any exchange (parse-guard).
        try:
            state_payload = verify_state(
                token=raw_state,
                signing_secret=cfg.signing_secret,
                now=time.time(),
                ttl_s=_CALLBACK_STATE_TTL_S,
            )
        except ValueError:
            state_payload = None
        if state_payload is None:
            return _error_html(kind="expired")

        if not code:
            return _error_html(kind="expired")

        async with client_factory() as http_client:
            try:
                result = await exchange_code(
                    client=http_client,
                    code=code,
                    client_id=cfg.client_id,
                    client_secret=cfg.client_secret,
                    redirect_url=cfg.redirect_url,
                )
            except (httpx.HTTPError, SlackOAuthError) as exc:
                logger.exception("slack token exchange failed")
                capture_exception_with_scope(exc)
                return _error_html(kind="exchange_failed")

        if state_payload.get("flow") == "user_connect":
            # D-06: only the bound Slack account may complete this link.
            if (
                result.team_id is None
                or result.team_id != state_payload.get("team_id")
                or result.authed_user_id is None
                or result.authed_user_id != state_payload.get("slack_user_id")
            ):
                if result.authed_user_access_token is not None:
                    # Slack already minted an xoxp token for the foreign user by
                    # the time we notice the mismatch. Best-effort revoke it so
                    # it isn't left live and unreferenced anywhere; nothing was
                    # ever persisted for it, so there's no row to clean up.
                    with contextlib.suppress(
                        SlackApiError, aiohttp.ClientError, asyncio.TimeoutError
                    ):
                        await AsyncWebClient(  # pyright: ignore[reportUnknownMemberType]
                            token=result.authed_user_access_token
                        ).auth_revoke()
                return _error_html(kind="wrong_account")
            if result.authed_user_access_token is None:
                # No exception object here (this is a shape check, not a caught
                # error) — logger.error stays, but carries team_id like the
                # exc_info-bearing exchange_failed branch above carries the
                # exception, so both failure paths are correlatable in logs.
                logger.error(
                    "slack user-connect exchange returned no authed_user token",
                    team_id=result.team_id,
                )
                return _error_html(kind="exchange_failed")
            user_expires_at = (
                datetime.now(tz=UTC) + timedelta(seconds=result.authed_user_expires_in)
                if result.authed_user_expires_in is not None
                else None
            )
            encrypted_user_refresh = (
                encrypt_token(cfg.fernet, result.authed_user_refresh_token)
                if result.authed_user_refresh_token is not None
                else None
            )
            async with sessionmaker.begin() as s:
                await upsert_slack_user_token(
                    s,
                    team_id=result.team_id,
                    slack_user_id=result.authed_user_id,
                    encrypted_token=encrypt_token(cfg.fernet, result.authed_user_access_token),
                    scopes=result.authed_user_scope or "",
                    expires_at=user_expires_at,
                    encrypted_refresh_token=encrypted_user_refresh,
                )
            return _connected_html()

        # T-79-05 / SINST-03: enterprise hard-reject BEFORE touching team_id (Pitfall 4).
        if result.is_enterprise_install:
            return _enterprise_rejection_html()

        if result.access_token is None:
            # An install exchange always carries a bot token; None means Slack
            # answered a shape we don't recognize — treat as exchange failure.
            logger.error("slack install exchange returned no bot access_token")
            return _error_html(kind="exchange_failed")

        # Non-enterprise path: team_id is non-None (D-04 contract).
        team_id = result.team_id
        if team_id is None:
            # SINST-03: team_id is None only for enterprise installs, rejected above.
            # This branch is unreachable in normal flow; guards the type narrowing.
            return _enterprise_rejection_html()

        await provision_tenant(
            sessionmaker,
            platform="slack",
            workspace_id=team_id,
            signup_credit=settings.billing.signup_credit,
        )

        expires_at = (
            datetime.now(tz=UTC) + timedelta(seconds=result.expires_in)
            if result.expires_in is not None
            else None
        )
        encrypted = encrypt_token(cfg.fernet, result.access_token)
        encrypted_refresh = (
            encrypt_token(cfg.fernet, result.refresh_token)
            if result.refresh_token is not None
            else None
        )

        async with sessionmaker.begin() as s:
            await upsert_slack_bot_token(
                s,
                team_id=team_id,
                encrypted_token=encrypted,
                expires_at=expires_at,
                refresh_token=encrypted_refresh,
            )

        return _success_html(
            workspace=result.team_name or team_id,
            signup_credit=settings.billing.signup_credit,
        )

    async def connect_handler(request: Request) -> Response:
        cfg = _resolve_config_or_error(settings, fernet)
        if isinstance(cfg, Response):
            return cfg
        raw_state = request.query_params.get("state", "")
        try:
            state_payload = verify_state(
                token=raw_state,
                signing_secret=cfg.signing_secret,
                now=time.time(),
                ttl_s=_CONNECT_STATE_TTL_S,
            )
        except ValueError:
            state_payload = None
        if state_payload is None or state_payload.get("flow") != "user_connect":
            return _error_html(kind="expired")
        authorize_url = build_authorize_url(
            client_id=cfg.client_id,
            redirect_url=cfg.redirect_url,
            state=raw_state,
            scopes=(),
            user_scope=SLACK_USER_SCOPES,
        )
        return RedirectResponse(authorize_url, status_code=302)

    return install_handler, callback_handler, connect_handler
