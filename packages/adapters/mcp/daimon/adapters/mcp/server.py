"""daimon-mcp ASGI factory.

Production deployment:
    uvicorn daimon.adapters.mcp.server:create_mcp_app --factory --port 8765

Factory validates required settings at boot. Tests swap in collaborators
individually via kwargs and the factory skips the env-var validation for
any collaborator the caller supplied.
"""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.checkout import billing_cancel, billing_success, build_checkout_route
from daimon.adapters.mcp.file_store import FileStore
from daimon.adapters.mcp.middleware.ma_errors import MaErrorMiddleware
from daimon.adapters.mcp.middleware.mcp_identity import (
    ClaimResolver,
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.adapters.mcp.oauth_slack import build_oauth_slack_routes
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.search_transform import AgentChatAwareBM25SearchTransform
from daimon.adapters.mcp.tools import (
    agent_chat,
    agents,
    environments,
    routines,
    self_edit,
    sessions,
    skills,
    time,
    vault,
)
from daimon.adapters.mcp.tools.channels import register_channel_tools
from daimon.adapters.mcp.tools.cli_token import register_cli_token_tool
from daimon.adapters.mcp.tools.media import register_media_tools
from daimon.adapters.mcp.tools.notebook import register_notebook_tools
from daimon.adapters.mcp.tools.propagation import register_propagation_tools
from daimon.adapters.mcp.webhooks import build_github_webhook, build_stripe_webhook
from daimon.core.billing import BillingConfig, load_billing_config
from daimon.core.config import Settings, load_settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.errors import BootstrapError
from daimon.core.github_credentials import build_multifernet
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.observability import init_sentry
from fastmcp import FastMCP
from fastmcp.server.auth.auth import TokenVerifier
from fastmcp.server.transforms import Visibility
from fastmcp.server.transforms.search.base import serialize_tools_for_output_markdown
from google import genai
from sentry_sdk.integrations.starlette import StarletteIntegration
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse

if TYPE_CHECKING:
    from stripe._http_client import HTTPClient as StripeHTTPClient

log = structlog.get_logger(__name__)

_MIN_SECRET_BYTES = 32

log = structlog.get_logger()


def _validate_settings(
    settings: Settings,
    *,
    skip_auth: bool,
) -> None:
    if not skip_auth:
        if settings.mcp.jwt_secret is None:
            raise BootstrapError("DAIMON_MCP__JWT_SECRET is required to run daimon-mcp")
        secret = settings.mcp.jwt_secret.get_secret_value()
        if len(secret.encode()) < _MIN_SECRET_BYTES:
            raise BootstrapError(
                f"DAIMON_MCP__JWT_SECRET must be at least {_MIN_SECRET_BYTES} bytes "
                f"(PyJWT HS256 InsecureKeyLengthWarning threshold)"
            )
    if settings.mcp.public_url is None:
        raise BootstrapError(
            "DAIMON_MCP__PUBLIC_URL is required — use the URL MA will dial, "
            "e.g. https://daimon-mcp.example.com/mcp"
        )


async def _healthz(_req: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _build_readyz(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[[Request], Awaitable[PlainTextResponse]]:
    async def readyz(_req: Request) -> PlainTextResponse:
        async with sessionmaker() as s:
            await s.execute(text("SELECT 1"))
        return PlainTextResponse("ready")

    return readyz


def create_mcp_app(
    *,
    settings: Settings | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    auth: TokenVerifier | None = None,
    subject_resolver: ClaimResolver | None = None,
    tenant_resolver: ClaimResolver | None = None,
    role_resolver: ClaimResolver | None = None,
    agent_id_resolver: ClaimResolver | None = None,
    is_admin_resolver: ClaimResolver | None = None,
    internal_resolver: ClaimResolver | None = None,
    anthropic: AsyncAnthropic | None = None,
    billing_config: BillingConfig | None = None,
    stripe_http_client: StripeHTTPClient | None = None,
) -> Starlette:
    """ASGI app factory.

    Production: all kwargs None → load_settings(), construct DaimonJWTVerifier
    + sessionmaker, install production subject_resolver.
    Tests: supply whichever collaborators they want to override.

    When `auth` is supplied (tests using StaticTokenVerifier) the JWT_SECRET
    validation is skipped; PUBLIC_URL is still enforced because
    `ensure_mcp_vault` needs it on the session-create side.
    """
    effective_settings = settings or load_settings()
    sentry_dsn = (
        effective_settings.sentry.dsn.get_secret_value() if effective_settings.sentry.dsn else None
    )
    init_sentry(
        dsn=sentry_dsn,
        environment=effective_settings.sentry.environment,
        process="mcp",
        release=None,
        traces_sample_rate=effective_settings.sentry.traces_sample_rate,
        integrations=[StarletteIntegration()],
    )
    _validate_settings(effective_settings, skip_auth=auth is not None)

    effective_sessionmaker = sessionmaker
    if effective_sessionmaker is None:
        engine = build_engine(str(effective_settings.database.url))
        effective_sessionmaker = build_session_factory(engine)

    effective_auth = auth
    if effective_auth is None:
        secret_value = effective_settings.mcp.jwt_secret
        if secret_value is None:
            # _validate_settings enforced this above; assert was stripped by
            # `python -O`. Use an explicit raise so the failure mode is real.
            raise BootstrapError("DAIMON_MCP__JWT_SECRET missing after validation")
        effective_auth = DaimonJWTVerifier(
            secret=secret_value.get_secret_value().encode(),
            sessionmaker=effective_sessionmaker,
        )

    effective_resolver = subject_resolver or production_subject_resolver
    effective_tenant_resolver = tenant_resolver or production_tenant_resolver
    effective_role_resolver = role_resolver or production_role_resolver
    effective_agent_id_resolver = agent_id_resolver or production_agent_id_resolver
    effective_is_admin_resolver = is_admin_resolver or production_is_admin_resolver
    effective_internal_resolver = internal_resolver or production_internal_resolver

    effective_anthropic = anthropic
    if effective_anthropic is None:
        effective_anthropic = AsyncAnthropic(
            api_key=effective_settings.anthropic.api_key.get_secret_value()
        )

    effective_billing_config = billing_config
    if effective_billing_config is None:
        # Billing is optional — load_billing_config() returns None when STRIPE_* vars are absent.
        # Stripe webhook route is only mounted when billing_config is not None (see below).
        effective_billing_config = load_billing_config()

    mcp = FastMCP(name="daimon", auth=effective_auth)
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=effective_resolver,
            tenant_resolver=effective_tenant_resolver,
            role_resolver=effective_role_resolver,
            agent_id_resolver=effective_agent_id_resolver,
            is_admin_resolver=effective_is_admin_resolver,
            internal_resolver=effective_internal_resolver,
            sessionmaker=effective_sessionmaker,
        )
    )
    # Tool-dispatch error boundary: convert upstream anthropic.APIError into a
    # structured ToolError instead of an opaque internal error (issue #14).
    mcp.add_middleware(MaErrorMiddleware())

    mcp.add_transform(Visibility(False, tags={"admin"}))
    mcp.add_transform(Visibility(False, tags={"agent-chat"}))
    mcp.add_transform(
        AgentChatAwareBM25SearchTransform(
            max_results=5,
            always_visible=["list_credentials"],
            search_result_serializer=serialize_tools_for_output_markdown,
        )
    )

    # Build Gemini client + FileStore when DAIMON_GEMINI__API_KEY is
    # configured. Both are optional — without a Gemini key the media tools
    # skip registration and the rest of the mcp surface boots normally.
    gemini_client: genai.Client | None = None
    file_store: FileStore | None = None
    file_store_dir: Path | None = None
    if effective_settings.gemini.api_key is not None:
        gemini_client = genai.Client(api_key=effective_settings.gemini.api_key.get_secret_value())
        file_store_dir = effective_settings.mcp.file_store_dir or (
            Path(tempfile.gettempdir()) / "daimon-mcp-files"
        )
        file_store = FileStore(base_dir=file_store_dir)

    notebook_rate_limiter = RateLimiter(
        max_requests=effective_settings.notebook.publish_rate_per_hour,
    )

    fernet = (
        build_multifernet(tuple(k.get_secret_value() for k in effective_settings.crypto.keys))
        if effective_settings.crypto.keys
        else None
    )

    deployment_default = parse_deployment_default(effective_settings.defaults_root)

    runtime = McpRuntime(
        session_factory=effective_sessionmaker,
        client=effective_anthropic,
        settings=effective_settings,
        deployment_default=deployment_default,
        gemini_client=gemini_client,
        file_store=file_store,
        notebook_rate_limiter=notebook_rate_limiter,
        fernet=fernet,
    )
    agents.register_agent_tools(mcp, runtime)
    environments.register_environment_tools(mcp, runtime)
    vault.register_vault_tools(mcp, runtime)
    skills.register_skill_tools(mcp, runtime)
    sessions.register_sessions_tools(mcp, runtime)
    agent_chat.register_agent_chat_tools(mcp, runtime)
    time.register_time_tools(mcp, runtime)
    routines.register_routines_tools(mcp, runtime)
    register_cli_token_tool(mcp, runtime)
    if effective_settings.discord is not None or effective_settings.slack is not None:
        register_channel_tools(mcp, runtime)
    else:
        log.info("channel tools disabled", reason="no discord or slack settings")
    self_edit.register_self_edit_tools(mcp, runtime)  # agent self-edit tools
    register_notebook_tools(mcp, runtime)  # notebook publish (raises when unconfigured)
    register_propagation_tools(mcp, runtime)  # set/clear agent default

    if gemini_client is not None and file_store is not None:
        register_media_tools(
            mcp,
            gemini_client=gemini_client,
            file_store=file_store,
            sessionmaker=effective_sessionmaker,
            billing_config=effective_billing_config,
            markup=effective_settings.billing.markup,
        )
        log.info("mcp.media_tools_registered", store_dir=str(file_store_dir))
    else:
        log.info("mcp.media_tools_skipped", reason="DAIMON_GEMINI__API_KEY not set")

    app = mcp.http_app()
    app.state.mcp = mcp
    app.add_route("/healthz", _healthz, methods=["GET"])
    app.add_route("/readyz", _build_readyz(effective_sessionmaker), methods=["GET"])
    if effective_billing_config is not None:
        app.add_route(
            "/webhooks/stripe",
            build_stripe_webhook(
                sessionmaker=effective_sessionmaker,
                billing_config=effective_billing_config,
            ),
            methods=["POST"],
        )
        # Construct StripeClient once, inject into checkout route (no module-level singleton).
        from stripe import StripeClient

        stripe_client = StripeClient(
            effective_billing_config.secret_key.get_secret_value(),
            http_client=stripe_http_client,  # None = default; tests inject a mock
        )
        app.add_route(
            "/billing/checkout",
            build_checkout_route(
                stripe_client=stripe_client,
                billing_config=effective_billing_config,
                auth=effective_auth,
            ),
            methods=["POST"],
        )
        app.add_route("/billing/success", billing_success, methods=["GET"])
        app.add_route("/billing/cancel", billing_cancel, methods=["GET"])

    # Slack install flow — only mounted when Slack OAuth settings and crypto are present.
    if effective_settings.slack is not None and fernet is not None:
        slack_install, slack_callback, slack_connect = build_oauth_slack_routes(
            sessionmaker=effective_sessionmaker,
            settings=effective_settings,
            fernet=fernet,
        )
        app.add_route("/oauth/slack/install", slack_install, methods=["GET"])
        app.add_route("/oauth/slack/callback", slack_callback, methods=["GET"])
        app.add_route("/oauth/slack/connect", slack_connect, methods=["GET"])

        proxy_secret = effective_settings.mcp.jwt_secret
        if proxy_secret is not None:
            from daimon.adapters.mcp.slack_file_proxy import (
                build_slack_file_proxy_route,
                fetch_slack_file,
            )

            _proxy_http = httpx.AsyncClient(timeout=30.0)

            async def _fetch(bot_token: str, file_id: str) -> tuple[bytes, str, str]:
                return await fetch_slack_file(_proxy_http, bot_token=bot_token, file_id=file_id)

            app.add_route(
                "/slack/file/{token}",
                build_slack_file_proxy_route(
                    sessionmaker=effective_sessionmaker,
                    fernet=fernet,
                    secret=proxy_secret.get_secret_value(),
                    fetch_file=_fetch,
                ),
                methods=["GET"],
            )
    else:
        log.info("slack oauth disabled", reason="no slack settings or crypto keys")

    # GitHub App clone-auth: App-clone boots with only app_id +
    # app_private_key — no webhook required. The /webhooks/github mount is
    # required only by skill-sync's push-driven resync and is gated separately
    # on webhook_secret (fail-fast on partial webhook config, never coupled to
    # App-clone itself).
    github_cfg = effective_settings.github
    app_configured = github_cfg.app_id is not None and github_cfg.app_private_key is not None
    if github_cfg.app_id is not None and github_cfg.app_private_key is None:
        raise BootstrapError(
            "GitHub App is partially configured: app_id is set but app_private_key "
            "is missing. Set DAIMON_GITHUB__APP_PRIVATE_KEY, or leave both unset to "
            "disable the GitHub App."
        )
    if github_cfg.webhook_secret is not None:
        if not app_configured:
            raise BootstrapError(
                "DAIMON_GITHUB__WEBHOOK_SECRET is set but the GitHub App is not "
                "configured (app_id + app_private_key required). Set both, or unset "
                "WEBHOOK_SECRET to disable the skill-sync webhook."
            )
        if fernet is None:
            raise BootstrapError(
                "DAIMON_GITHUB__WEBHOOK_SECRET is set but no crypto keys are set: "
                "push-driven skill sync cannot decrypt the MA/MCP credential without "
                "them. Set DAIMON_CRYPTO__KEYS, or unset WEBHOOK_SECRET to disable "
                "the webhook."
            )
        app.add_route(
            "/webhooks/github",
            build_github_webhook(
                sessionmaker=effective_sessionmaker,
                github_settings=github_cfg,
                anthropic=effective_anthropic,
                fernet=fernet,
            ),
            methods=["POST"],
        )

    return app
