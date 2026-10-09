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
from daimon.adapters.mcp.web_shell import render_page
from daimon.core.config import Settings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import SlackOAuthError
from daimon.core.github_credentials import encrypt_token
from daimon.core.observability import capture_exception_with_scope
from daimon.core.ops_alerts import alert_ops
from daimon.core.slack_customize_scope import clear_missing_customize_scope
from daimon.core.slack_oauth import (
    SLACK_USER_SCOPES,
    build_authorize_url,
    exchange_code,
    mint_state,
    slack_bot_scopes,
    verify_state,
)
from daimon.core.stores.promo_codes import has_redeemable_promo_code
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.slack_user_tokens import upsert_slack_user_token
from daimon.core.stores.tenants import set_provision_status
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = structlog.get_logger(__name__)

RouteHandler = Callable[[Request], Awaitable[Response]]
HttpxClientFactory = Callable[[], httpx.AsyncClient]

# ---------------------------------------------------------------------------
# Shared branded template helper
# ---------------------------------------------------------------------------


def _page(*, title: str, state_bar: str, body_html: str, status: int = 200) -> HTMLResponse:
    """Render Slack authorization content inside the shared MCP page shell."""
    return render_page(
        title=title,
        body_html=body_html,
        status=status,
        context="Slack",
        error="rose" in state_bar or status >= 400,
    )


def render_branded_page(
    *, title: str, state_bar: str, body_html: str, status: int = 200
) -> HTMLResponse:
    """Compatibility entry point for Slack authorization page rendering."""
    return _page(title=title, state_bar=state_bar, body_html=body_html, status=status)


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------

_SCOPES_HTML = """
<ul class="scope-list">
  <li>Read mentions <code>app_mentions:read</code></li>
  <li>Post replies <code>chat:write</code></li>
  <li>Run setup commands <code>commands</code></li>
  <li>Check admin status <code>users:read</code></li>
  <li>Read invited public channels <code>channels:history</code></li>
  <li>Read invited private channels <code>groups:history</code></li>
  <li>List channels and members <code>channels:read</code> <code>groups:read</code></li>
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
    display_name: str = "daimon",
) -> HTMLResponse:
    """Branded install landing page (Page 1 per UI-SPEC).

    Shows the 6 locked SLACK_BOT_SCOPES in plain language before the user
    clicks through to Slack's consent screen.
    """
    safe_href = html.escape(authorize_url, quote=True)
    credit = _format_credit(signup_credit)
    safe_name = html.escape(display_name, quote=False)
    body = f"""
<h1>Add Daimon to Slack</h1>
<p>Get {credit} in credit for analysis, code review and research.</p>
<h2>Slack access</h2>
{_SCOPES_HTML}
<a class="btn" href="{safe_href}">Add to Slack</a>
<p class="footer">{safe_name} reads invited channels. Members can connect their own
accounts for personal access.</p>
"""
    return _page(title="Add Daimon to Slack", state_bar="", body_html=body)


def _success_html(
    *,
    workspace: str,
    signup_credit: Decimal,
    display_name: str = "daimon",
    promo_codes: bool = False,
) -> HTMLResponse:
    """Branded success page (Page 2 per UI-SPEC).

    ``promo_codes``: some code is redeemable now, so point admins at /billing.

    `workspace` is the `team.name` from the Slack OAuth response —
    attacker-influenceable, so it is HTML-escaped before interpolation.
    """
    safe = html.escape(workspace, quote=False)
    credit = _format_credit(signup_credit)
    safe_name = html.escape(display_name, quote=False)
    promo = (
        "<p>have a promo code? admins can redeem it in <code>/billing</code>.</p>\n"
        if promo_codes
        else ""
    )
    body = f"""
<h1>Installed in {safe}</h1>
<p>Mention <code>@{safe_name}</code> in an invited channel, or run <code>/agent-setup</code>.</p>
<p>{credit} in credit is ready.</p>
{promo}<p class="dim">You can close this tab and return to Slack.</p>
"""
    return _page(title="Daimon installed", state_bar="", body_html=body)


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
# tighter TTL. This is fine by design: callback verification is
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
            scopes=slack_bot_scopes(identity_enabled=settings.agent_identity.enabled),
        )
        return _install_landing_html(
            authorize_url=authorize_url,
            signup_credit=settings.billing.signup_credit,
            display_name=settings.slack.bot_display_name
            if settings.slack is not None
            else "daimon",
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
            # only the bound Slack account may complete this link.
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

        # enterprise hard-reject BEFORE touching team_id.
        if result.is_enterprise_install:
            return _enterprise_rejection_html()

        if result.access_token is None:
            # An install exchange always carries a bot token; None means Slack
            # answered a shape we don't recognize — treat as exchange failure.
            logger.error("slack install exchange returned no bot access_token")
            return _error_html(kind="exchange_failed")

        # Non-enterprise path: team_id is non-None.
        team_id = result.team_id
        if team_id is None:
            # team_id is None only for enterprise installs, rejected above.
            # This branch is unreachable in normal flow; guards the type narrowing.
            return _enterprise_rejection_html()

        tenant = await provision_tenant(
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
        clear_missing_customize_scope(result.access_token)
        # A reinstall after an uninstall finds its tenant soft-archived, and
        # provision_tenant leaves an existing row alone. Clear it only after
        # the token is stored: teardown locks the tenant row and skips when
        # the stored token postdates its event, so a stale teardown landing
        # on either side of this clear cannot leave the workspace archived.
        await set_provision_status(sessionmaker, tenant_id=tenant.tenant_id, clear_archive=True)
        alert_ops(
            settings.ops.alert_webhook_url,
            key=f"install:slack:{team_id}",
            message=f"New install: Slack {result.team_name or team_id} ({team_id})",
        )
        try:
            async with sessionmaker() as s:
                promo_codes = await has_redeemable_promo_code(s, now=datetime.now(UTC))
        except SQLAlchemyError as exc:
            # The install is done; a failed lookup only drops the promo line.
            logger.warning(
                "slack install promo code lookup failed", team_id=team_id, error=str(exc)
            )
            promo_codes = False

        return _success_html(
            workspace=result.team_name or team_id,
            signup_credit=settings.billing.signup_credit,
            display_name=settings.slack.bot_display_name
            if settings.slack is not None
            else "daimon",
            promo_codes=promo_codes,
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
