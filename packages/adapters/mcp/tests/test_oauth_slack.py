"""Tests for oauth_slack.py: branded HTML pages (unit) and route handlers (integration).

Unit tests cover:
- _page shell: doctype, :root token block, status stripe color
- _install_landing_html: 6 locked scope ids present
- _success_html: workspace name is HTML-escaped (XSS prevention)
- _enterprise_rejection_html: reassurance copy present
- _error_html: correct HTTP statuses and static copy only

Integration tests cover:
- install_handler: returns landing page with authorize URL and signed state
- callback_handler: persists encrypted token + tenant; enterprise rejected; rotation fields;
  bad state → 400; replay idempotent
- server.py mount: routes present when configured, absent (404) when unconfigured
"""

from __future__ import annotations

import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
from aioresponses import aioresponses
from cryptography.fernet import Fernet
from daimon.adapters.mcp.oauth_slack import (
    _enterprise_rejection_html,
    _error_html,
    _install_landing_html,
    _page,
    _success_html,
    build_oauth_slack_routes,
)
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core._models import SlackBotToken
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
    SlackSettings,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.slack_oauth import SLACK_USER_SCOPES, mint_state
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.slack_user_tokens import get_slack_user_token
from daimon.core.stores.tenant_ledger import get_balance
from daimon.core.stores.tenants import list_tenants_by_platform
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

# ---------------------------------------------------------------------------
# _page shell tests
# ---------------------------------------------------------------------------


def test_page_shell_contains_doctype() -> None:
    response = _page(title="test page", state_bar="", body_html="<p>hello</p>")
    body = response.body.decode()
    assert "<!doctype html>" in body, "_page should emit an HTML5 doctype"


def test_page_shell_contains_root_tokens() -> None:
    response = _page(title="test page", state_bar="", body_html="<p>hello</p>")
    body = response.body.decode()
    assert "--stage: #0b110e" in body, "_page CSS should declare --stage token"
    assert "--accent: #44a171" in body, "_page CSS should declare --accent token"
    assert "--rose: #c68d8c" in body, "_page CSS should declare --rose token"
    assert "--accent-press: #2f8e60" in body, "_page CSS should declare --accent-press token"
    assert "--stage-raised: #141d19" in body, "_page CSS should declare --stage-raised token"


def test_page_shell_contains_viewport_meta() -> None:
    response = _page(title="test page", state_bar="", body_html="<p>hello</p>")
    body = response.body.decode()
    assert "width=device-width" in body, "_page should include viewport meta"


def test_page_shell_button_min_height() -> None:
    response = _page(title="test page", state_bar="", body_html="<p>hello</p>")
    body = response.body.decode()
    assert "min-height: 44px" in body, "_page should declare 44px touch-target floor on button"


def test_page_shell_system_ui_fallback() -> None:
    response = _page(title="test page", state_bar="", body_html="<p>hello</p>")
    body = response.body.decode()
    assert "system-ui" in body, "_page font-family stacks should end in system-ui fallback"


def test_page_shell_accent_stripe_color() -> None:
    response = _page(title="test page", state_bar="", body_html="<p>hello</p>")
    body = response.body.decode()
    # accent stripe: the status-bar div uses the plain class (no modifier suffix)
    assert 'class="status-bar"' in body, "accent state_bar should render plain status-bar class"


def test_page_shell_rose_stripe_color() -> None:
    response = _page(title="test page", state_bar=" status-bar--rose", body_html="<p>err</p>")
    body = response.body.decode()
    assert "status-bar--rose" in body, "rose state_bar should render status-bar--rose modifier"


def test_page_shell_status_code() -> None:
    ok = _page(title="t", state_bar="", body_html="", status=200)
    err = _page(title="t", state_bar="", body_html="", status=400)
    assert ok.status_code == 200, "_page should pass through the status argument"
    assert err.status_code == 400, "_page should pass through the status argument"


# ---------------------------------------------------------------------------
# _install_landing_html tests
# ---------------------------------------------------------------------------


def test_install_landing_html_contains_all_six_scope_ids() -> None:
    response = _install_landing_html(
        authorize_url="https://slack.com/oauth/v2/authorize", signup_credit=Decimal("5.00")
    )
    body = response.body.decode()
    expected_scopes = [
        "app_mentions:read",
        "chat:write",
        "commands",
        "users:read",
        "channels:history",
        "groups:history",
        "channels:read",
        "groups:read",
    ]
    for scope in expected_scopes:
        assert scope in body, f"install landing should list scope {scope!r}"


def test_install_landing_html_title() -> None:
    response = _install_landing_html(
        authorize_url="https://slack.com/oauth/v2/authorize", signup_credit=Decimal("5.00")
    )
    body = response.body.decode()
    assert "<title>add daimon to Slack</title>" in body, "install landing should have correct title"


def test_install_landing_html_cta_label() -> None:
    response = _install_landing_html(
        authorize_url="https://slack.com/oauth/v2/authorize?foo=bar", signup_credit=Decimal("5.00")
    )
    body = response.body.decode()
    assert "Add to Slack" in body, "install landing CTA should say 'Add to Slack'"


def test_install_landing_html_authorize_url_is_attribute_escaped() -> None:
    # authorize URL may contain & in query string — must be &amp; in href attr
    url = "https://slack.com/oauth/v2/authorize?client_id=1&scope=chat:write"
    response = _install_landing_html(authorize_url=url, signup_credit=Decimal("5.00"))
    body = response.body.decode()
    assert "client_id=1&amp;scope" in body, (
        "& in authorize URL should be attribute-escaped to &amp;"
    )
    # raw & must NOT appear in attribute
    assert 'href="https://slack.com/oauth/v2/authorize?client_id=1&scope' not in body, (
        "raw & must not appear in href attribute"
    )


def test_install_landing_html_status_200() -> None:
    response = _install_landing_html(
        authorize_url="https://slack.com/oauth/v2/authorize", signup_credit=Decimal("5.00")
    )
    assert response.status_code == 200, "install landing should return 200"


# ---------------------------------------------------------------------------
# _success_html tests (XSS escaping — T-79-04)
# ---------------------------------------------------------------------------


def test_success_html_escapes_workspace_name_xss() -> None:
    """_success_html must HTML-escape the workspace name (attacker-influenceable).

    Sending a <script> payload must produce &lt;script&gt; in the body,
    never the raw tag that a browser would execute.
    """
    response = _success_html(workspace="<script>alert(1)</script>", signup_credit=Decimal("5.00"))
    body = response.body.decode()
    assert "<script>alert(1)</script>" not in body, (
        "raw <script> must not appear in success page body (XSS)"
    )
    assert "&lt;script&gt;" in body, "escaped &lt;script&gt; must appear in success page body"


def test_success_html_title() -> None:
    response = _success_html(workspace="Acme Corp", signup_credit=Decimal("5.00"))
    body = response.body.decode()
    assert "<title>daimon installed</title>" in body, "success page should have correct title"


def test_success_html_interpolates_workspace_name() -> None:
    response = _success_html(workspace="Acme Corp", signup_credit=Decimal("5.00"))
    body = response.body.decode()
    assert "installed in Acme Corp" in body, "success page h1 should include workspace name"


def test_success_html_no_button() -> None:
    response = _success_html(workspace="Acme Corp", signup_credit=Decimal("5.00"))
    body = response.body.decode()
    # The success page deliberately has no CTA button
    assert 'class="btn"' not in body, "success page should not have a CTA button"


def test_success_html_close_tab_copy() -> None:
    response = _success_html(workspace="Acme Corp", signup_credit=Decimal("5.00"))
    body = response.body.decode()
    assert "close this tab" in body, "success page should tell the user they can close the tab"


def test_success_html_status_200() -> None:
    response = _success_html(workspace="Acme Corp", signup_credit=Decimal("5.00"))
    assert response.status_code == 200, "success page should return 200"


# ---------------------------------------------------------------------------
# _enterprise_rejection_html tests
# ---------------------------------------------------------------------------


def test_enterprise_rejection_html_title() -> None:
    response = _enterprise_rejection_html()
    body = response.body.decode()
    assert "workspace install required" in body, (
        "rejection page title should mention workspace install"
    )


def test_enterprise_rejection_html_reassurance_nothing_stored() -> None:
    response = _enterprise_rejection_html()
    body = response.body.decode()
    # UI-SPEC: emphasized copy stating nothing was stored
    assert "no token" in body, "rejection page must reassure that no token was stored"
    assert "no data was stored" in body, "rejection page must reassure that no data was stored"


def test_enterprise_rejection_html_rose_stripe() -> None:
    response = _enterprise_rejection_html()
    body = response.body.decode()
    assert "status-bar--rose" in body, "rejection page should use the rose error stripe"


def test_enterprise_rejection_html_status_200() -> None:
    response = _enterprise_rejection_html()
    assert response.status_code == 200, "rejection page should return 200 (not an HTTP error)"


# ---------------------------------------------------------------------------
# _error_html tests (T-79-09 — static copy, no external values echoed)
# ---------------------------------------------------------------------------


def test_error_html_expired_status_400() -> None:
    response = _error_html(kind="expired")
    assert response.status_code == 400, "expired error should return 400"


def test_error_html_unconfigured_status_500() -> None:
    response = _error_html(kind="unconfigured")
    assert response.status_code == 500, "unconfigured error should return 500"


def test_error_html_exchange_failed_status_502() -> None:
    response = _error_html(kind="exchange_failed")
    assert response.status_code == 502, "exchange_failed error should return 502"


def test_error_html_expired_copy() -> None:
    response = _error_html(kind="expired")
    body = response.body.decode()
    assert "install link expired" in body, "expired error should state link expired"
    assert "start over" in body, "expired error should suggest starting over"


def test_error_html_unconfigured_copy() -> None:
    response = _error_html(kind="unconfigured")
    body = response.body.decode()
    assert "isn't set up here" in body, "unconfigured error should explain setup missing"
    assert "operator" in body, "unconfigured error should reference the operator"


def test_error_html_exchange_failed_copy() -> None:
    response = _error_html(kind="exchange_failed")
    body = response.body.decode()
    assert "Slack couldn" in body, "exchange_failed error should blame Slack"
    assert "try installing again" in body, "exchange_failed error should suggest retry"


def test_error_html_does_not_echo_exception_text() -> None:
    """_error_html must never interpolate exception text or request params.

    The static copies must not contain the words 'code', 'state', or 'error'
    as raw request-parameter keys (T-79-09).
    """
    for kind in ("expired", "unconfigured", "exchange_failed"):
        response = _error_html(kind=kind)  # type: ignore[arg-type]
        body = response.body.decode()
        # The page body must not contain these query-param key names
        # (the h1/p copy may contain them as substring of other words,
        # so we check for the standalone forms we care about)
        assert "?code=" not in body, f"{kind}: error page must not echo code param"
        assert "?state=" not in body, f"{kind}: error page must not echo state param"
        assert "?error=" not in body, f"{kind}: error page must not echo error param"
        assert "Traceback" not in body, f"{kind}: error page must not contain traceback"
        assert "Exception" not in body, f"{kind}: error page must not contain exception class"


def test_error_html_rose_stripe() -> None:
    for kind in ("expired", "unconfigured", "exchange_failed"):
        response = _error_html(kind=kind)  # type: ignore[arg-type]
        body = response.body.decode()
        assert "status-bar--rose" in body, f"{kind}: error page should use rose stripe"


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------

_SIGNING_SECRET = "signing-secret-32-bytes-long-xxx"
_SLACK_TEAM_ID = "T12345ABCDE"
_SLACK_TEAM_NAME = "Acme Corp"
_SLACK_CODE = "slack-auth-code-abc"


def _build_slack_settings(*, with_slack: bool = True, with_crypto: bool = True) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(
            jwt_secret=SecretStr("a" * 32),
            public_url=HttpUrl("https://x/mcp"),
        ),
        slack=(
            SlackSettings(
                signing_secret=SecretStr(_SIGNING_SECRET),
                app_token=SecretStr("xapp-test"),
                client_id="slack-client-id",
                client_secret=SecretStr("slack-client-secret"),
            )
            if with_slack
            else None
        ),
        crypto=(
            CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),))
            if with_crypto
            else CryptoSettings()
        ),
    )


def _build_isolated_slack_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    fernet: object,
    client_factory: object,
) -> Starlette:
    """Mount Slack OAuth routes directly on a bare Starlette app for full DI control."""
    install, callback, connect = build_oauth_slack_routes(
        sessionmaker=sessionmaker,
        settings=settings,
        fernet=fernet,  # type: ignore[arg-type]
        client_factory=client_factory,  # type: ignore[arg-type]
    )
    return Starlette(
        routes=[
            Route("/oauth/slack/install", install, methods=["GET"]),
            Route("/oauth/slack/callback", callback, methods=["GET"]),
            Route("/oauth/slack/connect", connect, methods=["GET"]),
        ]
    )


def _build_full_slack_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    with_slack: bool = True,
    with_crypto: bool = True,
) -> Starlette:
    """Build the full MCP app to test the conditional server.py mount."""
    return create_mcp_app(
        settings=_build_slack_settings(with_slack=with_slack, with_crypto=with_crypto),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )


def _make_slack_exchange_handler(
    *,
    team_id: str = _SLACK_TEAM_ID,
    team_name: str = _SLACK_TEAM_NAME,
    is_enterprise: bool = False,
    expires_in: int | None = None,
    refresh_token: str | None = None,
    authed_user: dict[str, object] | None = None,
) -> object:
    """Return a httpx.MockTransport handler that simulates oauth.v2.access."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slack.com" and request.url.path == "/api/oauth.v2.access":
            if is_enterprise:
                body = {
                    "ok": True,
                    "access_token": "xoxb-enterprise-token",
                    "is_enterprise_install": True,
                    "enterprise": {"id": "E12345"},
                }
            else:
                body: dict[str, object] = {
                    "ok": True,
                    "access_token": "xoxb-bot-token",
                    "team": {"id": team_id, "name": team_name},
                    "is_enterprise_install": False,
                    "bot_user_id": "U12345",
                }
                if expires_in is not None:
                    body["expires_in"] = expires_in
                if refresh_token is not None:
                    body["refresh_token"] = refresh_token
                if authed_user is not None:
                    body["authed_user"] = authed_user
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "unhandled"})

    return handler


# ---------------------------------------------------------------------------
# Integration tests: route handlers
# ---------------------------------------------------------------------------


async def test_install_landing_has_authorize_url_and_state(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """install_handler returns 200 with a branded page containing a signed-state authorize URL."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0)

    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/oauth/slack/install")

    assert r.status_code == 200, f"install handler should return 200; got {r.status_code}: {r.text}"
    body = r.text
    assert "slack.com/oauth/v2/authorize" in body, "install page must contain Slack authorize URL"
    assert "state=" in body, "install page must carry a signed state parameter"
    assert "Add to Slack" in body, "install page must have the 'Add to Slack' CTA"


async def test_callback_persists_token_and_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Happy-path callback: token + tenant persisted, success page shows workspace name."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    handler = _make_slack_exchange_handler()

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)  # type: ignore[arg-type]

    state = mint_state(signing_secret=_SIGNING_SECRET, now=time.time())
    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state={state}")

    assert r.status_code == 200, (
        f"happy-path callback should return 200; got {r.status_code}: {r.text}"
    )
    assert _SLACK_TEAM_NAME in r.text, (
        "success page must show the workspace name from the exchange response"
    )

    # Token row persisted.
    async with sessionmaker() as session:
        token_row = await get_slack_bot_token(session, team_id=_SLACK_TEAM_ID)
    assert token_row is not None, "callback must persist a slack_bot_tokens row"
    assert token_row.encrypted_token != b"xoxb-bot-token", (
        "stored token must be Fernet ciphertext, NOT plaintext"
    )

    # Tenant provisioned.
    expected_tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_SLACK_TEAM_ID)
    slack_tenants = await list_tenants_by_platform(sessionmaker, platform="slack")
    assert any(t.id == expected_tenant_id for t in slack_tenants), (
        "callback must provision a slack tenant keyed on the team_id"
    )


async def test_callback_grants_advertised_signup_credit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The install pages advertise a credit; the callback must actually grant it.

    Regression for the omitted signup_credit argument: provision_tenant defaults
    to 0, so the advertised trial credit was never seeded. The granted balance
    must equal the configured settings.billing.signup_credit.
    """
    settings = _build_slack_settings()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    handler = _make_slack_exchange_handler()

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)  # type: ignore[arg-type]

    state = mint_state(signing_secret=_SIGNING_SECRET, now=time.time())
    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state={state}")
    assert r.status_code == 200, f"happy-path callback should return 200; got {r.text}"

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_SLACK_TEAM_ID)
    async with sessionmaker() as session:
        balance = await get_balance(session, tenant_id=tenant_id)
    assert balance == settings.billing.signup_credit, (
        "callback must grant the advertised signup credit, not $0"
    )
    assert settings.billing.signup_credit > 0, (
        "test precondition: configured signup credit should be non-zero"
    )


async def test_enterprise_install_rejected_stores_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Enterprise install: rejection page rendered, zero tokens stored, zero tenants created."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    handler = _make_slack_exchange_handler(is_enterprise=True)

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)  # type: ignore[arg-type]

    state = mint_state(signing_secret=_SIGNING_SECRET, now=time.time())
    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state={state}")

    assert r.status_code == 200, "enterprise rejection page is HTTP 200 (not an error status)"
    assert "org-level" in r.text or "Enterprise" in r.text or "workspace install" in r.text, (
        "rejection page must contain enterprise-rejection copy"
    )

    # Zero token rows AND zero slack tenant rows.
    async with sessionmaker() as session:
        token_count = (
            await session.execute(select(func.count()).select_from(SlackBotToken))
        ).scalar_one()
    slack_tenants = await list_tenants_by_platform(sessionmaker, platform="slack")
    assert token_count == 0, "no slack_bot_tokens row should be stored for enterprise install"
    assert len(slack_tenants) == 0, "no slack tenant should be created for enterprise install"


async def test_callback_persists_rotation_fields(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """When exchange returns expires_in + refresh_token, both are stored (encrypted)."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    handler = _make_slack_exchange_handler(expires_in=43200, refresh_token="xoxe-refresh-token")

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)  # type: ignore[arg-type]

    state = mint_state(signing_secret=_SIGNING_SECRET, now=time.time())
    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state={state}")

    assert r.status_code == 200, f"rotation-field callback should return 200; got {r.text}"

    async with sessionmaker() as session:
        token_row = await get_slack_bot_token(session, team_id=_SLACK_TEAM_ID)
    assert token_row is not None, "token row must be stored for rotation-field callback"
    assert token_row.expires_at is not None, (
        "expires_at must be persisted when exchange returns expires_in"
    )
    assert token_row.refresh_token is not None, (
        "refresh_token must be persisted (encrypted) when exchange returns refresh_token"
    )
    assert token_row.refresh_token != b"xoxe-refresh-token", (
        "stored refresh_token must be Fernet ciphertext, NOT plaintext"
    )


async def test_callback_bad_state_returns_400(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A tampered or expired state yields a branded 400."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0)

    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r_tampered = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state=bad-state")
        r_empty = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state=")

    assert r_tampered.status_code == 400, "tampered state must return 400"
    assert r_empty.status_code == 400, "empty state must return 400"
    assert "install link expired" in r_tampered.text or "expired" in r_tampered.text, (
        "400 body must contain the 'expired' error copy"
    )


async def test_callback_replay_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A replayed valid callback (same state, same code) is idempotent: one tenant, token present."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    handler = _make_slack_exchange_handler()

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)  # type: ignore[arg-type]

    # Mint a state valid for both calls (no single-use enforcement).
    state = mint_state(signing_secret=_SIGNING_SECRET, now=time.time())
    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state={state}")
        r2 = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state={state}")

    assert r1.status_code == 200, f"first callback should return 200; got {r1.text}"
    assert r2.status_code == 200, (
        f"replayed callback should also return 200 (idempotent); got {r2.text}"
    )

    # Exactly one tenant and one token row.
    slack_tenants = await list_tenants_by_platform(sessionmaker, platform="slack")
    async with sessionmaker() as session:
        token_row = await get_slack_bot_token(session, team_id=_SLACK_TEAM_ID)
    assert len(slack_tenants) == 1, (
        f"replay must not create duplicate tenants; found {len(slack_tenants)}"
    )
    assert token_row is not None, "token row must be present after replay"


# ---------------------------------------------------------------------------
# Integration tests: server.py conditional mount
# ---------------------------------------------------------------------------


async def test_slack_routes_mount_when_configured(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """With Slack settings present, GET /oauth/slack/install is reachable (200)."""
    app = _build_full_slack_app(sessionmaker, with_slack=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/oauth/slack/install")
    assert r.status_code == 200, (
        f"Slack routes should be mounted and return 200 when configured; got {r.status_code}"
    )


async def test_slack_routes_not_mounted_without_slack_settings(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Without Slack settings, /oauth/slack/install is NOT mounted (404)."""
    app = _build_full_slack_app(sessionmaker, with_slack=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/oauth/slack/install")
    assert r.status_code == 404, (
        f"Slack routes must NOT be mounted when settings.slack is None; got {r.status_code}"
    )


# ---------------------------------------------------------------------------
# Integration tests: user-connect route handler (/oauth/slack/connect)
# ---------------------------------------------------------------------------

_SLACK_USER_ID = "U1"
_SLACK_FOREIGN_USER_ID = "U2"


async def test_connect_route_redirects_to_slack_with_user_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A valid user_connect state 302s to Slack's authorize URL with user_scope, no bot scope."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0)

    state = mint_state(
        signing_secret=_SIGNING_SECRET,
        now=time.time(),
        payload={
            "flow": "user_connect",
            "team_id": _SLACK_TEAM_ID,
            "slack_user_id": _SLACK_USER_ID,
        },
    )
    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/oauth/slack/connect", params={"state": state})

    assert resp.status_code == 302, "valid user_connect state must 302 to Slack authorize"
    location = resp.headers["location"]
    parsed = parse_qs(urlparse(location).query)
    assert parsed["user_scope"] == [",".join(SLACK_USER_SCOPES)], (
        "authorize URL must request the full SLACK_USER_SCOPES set"
    )
    assert "scope" not in parsed, "connect flow must not re-request bot scopes"
    assert parsed["state"] == [state], "the signed state must ride through unchanged"


async def test_connect_route_rejects_missing_or_foreign_state(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Garbage/unverifiable state on the connect route renders the expired page (400)."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0)

    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/oauth/slack/connect", params={"state": "garbage.token"})

    assert resp.status_code == 400, "unverifiable state must render the expired page"


# ---------------------------------------------------------------------------
# Integration tests: callback user_connect branch
# ---------------------------------------------------------------------------


async def test_callback_user_connect_stores_token_for_matching_user(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Matching authed_user lands on the connected page and upserts the xoxp ciphertext."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    handler = _make_slack_exchange_handler(
        authed_user={
            "id": _SLACK_USER_ID,
            "access_token": "xoxp-user-token",
            "scope": ",".join(SLACK_USER_SCOPES),
        }
    )

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)  # type: ignore[arg-type]

    state = mint_state(
        signing_secret=_SIGNING_SECRET,
        now=time.time(),
        payload={
            "flow": "user_connect",
            "team_id": _SLACK_TEAM_ID,
            "slack_user_id": _SLACK_USER_ID,
        },
    )
    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state={state}")

    assert resp.status_code == 200 and "connected" in resp.text.lower(), (
        "matching authed_user must land on the connected page"
    )

    async with sessionmaker() as s:
        row = await get_slack_user_token(s, team_id=_SLACK_TEAM_ID, slack_user_id=_SLACK_USER_ID)
    assert row is not None, "xoxp ciphertext must be upserted for (T1, U1)"
    assert row.encrypted_token != b"xoxp-user-token", (
        "stored user token must be Fernet ciphertext, NOT plaintext"
    )


async def test_callback_user_connect_rejects_mismatched_user_and_stores_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A state bound to one Slack user completed by a different authed_user is rejected."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    handler = _make_slack_exchange_handler(
        authed_user={
            "id": _SLACK_FOREIGN_USER_ID,
            "access_token": "xoxp-foreign-token",
            "scope": ",".join(SLACK_USER_SCOPES),
        }
    )

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)  # type: ignore[arg-type]

    # state binds U1; the exchange answers authed_user id U2.
    state = mint_state(
        signing_secret=_SIGNING_SECRET,
        now=time.time(),
        payload={
            "flow": "user_connect",
            "team_id": _SLACK_TEAM_ID,
            "slack_user_id": _SLACK_USER_ID,
        },
    )
    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state={state}")

    assert resp.status_code == 400, "authed_user mismatch must reject"
    assert "different slack user" in resp.text.lower(), "static wrong-account copy shown"

    async with sessionmaker() as s:
        row_u1 = await get_slack_user_token(s, team_id=_SLACK_TEAM_ID, slack_user_id=_SLACK_USER_ID)
        row_u2 = await get_slack_user_token(
            s, team_id=_SLACK_TEAM_ID, slack_user_id=_SLACK_FOREIGN_USER_ID
        )
    assert row_u1 is None, "no token row should be stored for the bound (mismatched) user"
    assert row_u2 is None, "no token row should be stored for the foreign authed_user either"


async def test_callback_user_connect_mismatch_attempts_revoke_and_still_returns_400(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """By mismatch time Slack already minted the foreign user's xoxp token —
    the callback must best-effort auth.revoke it, and still render the 400
    wrong_account page even when that revoke call itself fails."""
    fernet_key = Fernet.generate_key().decode()
    settings = _build_slack_settings()
    fernet = build_multifernet((fernet_key,))

    handler = _make_slack_exchange_handler(
        authed_user={
            "id": _SLACK_FOREIGN_USER_ID,
            "access_token": "xoxp-foreign-token",
            "scope": ",".join(SLACK_USER_SCOPES),
        }
    )

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)  # type: ignore[arg-type]

    state = mint_state(
        signing_secret=_SIGNING_SECRET,
        now=time.time(),
        payload={
            "flow": "user_connect",
            "team_id": _SLACK_TEAM_ID,
            "slack_user_id": _SLACK_USER_ID,
        },
    )
    app = _build_isolated_slack_app(
        sessionmaker, settings=settings, fernet=fernet, client_factory=make_client
    )
    transport = httpx.ASGITransport(app=app)
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            "https://slack.com/api/auth.revoke",
            payload={"ok": False, "error": "invalid_auth"},
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/oauth/slack/callback?code={_SLACK_CODE}&state={state}")

    assert resp.status_code == 400, "revoke failing must not prevent the wrong_account page"
    assert "different slack user" in resp.text.lower(), "static wrong-account copy still shown"
    revoke_calls = [
        req
        for (method, url), reqs in m.requests.items()
        if method == "GET" and url.path == "/api/auth.revoke"
        for req in reqs
    ]
    assert len(revoke_calls) == 1, "callback must attempt exactly one best-effort auth.revoke"
    async with sessionmaker() as s:
        row_u2 = await get_slack_user_token(
            s, team_id=_SLACK_TEAM_ID, slack_user_id=_SLACK_FOREIGN_USER_ID
        )
    assert row_u2 is None, "no token row should ever be stored for the foreign authed_user"
