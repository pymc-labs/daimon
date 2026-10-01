"""Assemble and mount the per-platform hub apps.

Each platform gets its own ``FastMCP`` with its own OAuth proxy as ``auth``,
so a Slack login token is never evaluated by the Discord proxy and vice versa.
Both share the runtime with the JWT-verified app at ``/mcp``.

Mounting is more than ``Mount()``: Starlette does not run a sub-app's
lifespan, and RFC 8414 discovery for ``https://host/discord/mcp`` is fetched
from ``https://host/.well-known/oauth-authorization-server/discord``, at the
origin root. ``mount_hub_apps`` therefore chains the sub-app lifespans into the
parent's and re-roots each provider's well-known routes on the parent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import structlog
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.hub.discord_provider import DaimonDiscordProvider
from daimon.adapters.mcp.hub.identity import HubIdentityMiddleware
from daimon.adapters.mcp.hub.slack_provider import SlackHubProvider
from daimon.adapters.mcp.hub.storage import asyncpg_dsn, build_hub_kv_base, hub_kv_for
from daimon.adapters.mcp.middleware.ma_errors import MaErrorMiddleware
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.hub import register_hub_tools
from daimon.core.billing import BillingConfig
from daimon.core.config import Settings
from daimon.core.errors import BootstrapError
from daimon.core.stores.domain import Platform
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AuthProvider
from key_value.aio.protocols import AsyncKeyValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Mount

log = structlog.get_logger(__name__)

HUB_MCP_PATH = "/mcp"


def build_hub_app(
    *,
    platform: Platform,
    runtime: McpRuntime,
    auth: AuthProvider,
    billing_config: BillingConfig | None,
) -> FastMCP:
    mcp = FastMCP(
        name=f"daimon-{platform}",
        auth=auth,
        instructions=(
            f"Every daimon reachable here lives in a {platform.title()} workspace. "
            "Call list_daimons first; every other tool takes a daimon_id from that list."
        ),
    )
    mcp.add_middleware(HubIdentityMiddleware(platform))
    mcp.add_middleware(MaErrorMiddleware())
    register_hub_tools(mcp, runtime, billing_config=billing_config)
    return mcp


def _providers(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    hub_kv: AsyncKeyValue,
    signing_key: bytes,
    app_root_url: str,
) -> list[tuple[Platform, AuthProvider]]:
    out: list[tuple[Platform, AuthProvider]] = []
    hub = settings.hub
    if hub.slack_configured:
        assert hub.slack_client_id is not None and hub.slack_client_secret is not None
        out.append(
            (
                "slack",
                SlackHubProvider(
                    client_id=hub.slack_client_id,
                    client_secret=hub.slack_client_secret.get_secret_value(),
                    base_url=f"{app_root_url}/slack",
                    session_factory=sessionmaker,
                    client_storage=hub_kv_for(hub_kv, platform="slack"),
                    jwt_signing_key=signing_key,
                    allowed_client_redirect_uris=hub.allowed_client_redirect_uris,
                ),
            )
        )
    if hub.discord_configured:
        assert hub.discord_client_id is not None and hub.discord_client_secret is not None
        out.append(
            (
                "discord",
                DaimonDiscordProvider(
                    client_id=hub.discord_client_id,
                    client_secret=hub.discord_client_secret.get_secret_value(),
                    base_url=f"{app_root_url}/discord",
                    session_factory=sessionmaker,
                    client_storage=hub_kv_for(hub_kv, platform="discord"),
                    jwt_signing_key=signing_key,
                    allowed_client_redirect_uris=hub.allowed_client_redirect_uris,
                ),
            )
        )
    return out


def mount_hub_apps(
    app: Starlette,
    *,
    settings: Settings,
    runtime: McpRuntime,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    fernet: MultiFernet | None,
) -> list[str]:
    """Mount configured hub apps on ``app``. Returns the platforms mounted."""
    hub = settings.hub
    if not (hub.slack_configured or hub.discord_configured):
        return []
    if hub.jwt_signing_key is None:
        raise BootstrapError(
            "DAIMON_HUB__JWT_SIGNING_KEY is required when a hub mount is configured"
        )
    if fernet is None:
        raise BootstrapError("DAIMON_CRYPTO__KEYS is required when a hub mount is configured")
    app_root_url = settings.mcp.app_root_url
    if app_root_url is None:
        raise BootstrapError("DAIMON_MCP__PUBLIC_URL is required when a hub mount is configured")

    signing_key = hub.jwt_signing_key.get_secret_value().encode()
    kv_store, hub_kv = build_hub_kv_base(
        database_url=asyncpg_dsn(str(settings.database.url)), fernet=fernet
    )
    sub_apps: list[Starlette] = []
    mounted: list[str] = []
    for platform, provider in _providers(
        settings=settings,
        sessionmaker=sessionmaker,
        hub_kv=hub_kv,
        signing_key=signing_key,
        app_root_url=app_root_url,
    ):
        mcp = build_hub_app(
            platform=platform, runtime=runtime, auth=provider, billing_config=billing_config
        )
        sub_app = mcp.http_app(path=HUB_MCP_PATH, stateless_http=True, json_response=True)
        for route in provider.get_well_known_routes(mcp_path=HUB_MCP_PATH):
            app.router.routes.append(route)
        app.router.routes.append(Mount(f"/{platform}", app=sub_app))
        sub_apps.append(sub_app)
        mounted.append(platform)
        log.info(
            "hub.mounted", platform=platform, endpoint=f"{app_root_url}/{platform}{HUB_MCP_PATH}"
        )

    parent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def chained(a: Starlette) -> AsyncIterator[Any]:
        async with AsyncExitStack() as stack:
            state = await stack.enter_async_context(parent_lifespan(a))
            await stack.enter_async_context(kv_store)
            for sub in sub_apps:
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield state

    app.router.lifespan_context = chained
    return mounted
