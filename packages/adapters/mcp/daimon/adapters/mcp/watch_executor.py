"""Private poll-only ASGI listener. Never mount this app on the public MCP app.

Compose exposes no host port or proxy route. A separate shared secret is checked
before capability lookup and budgets. This runtime can only retrieve agent facts
and execute the operator's registry of read tools; it registers no MCP tools.
It decrypts at most one agent-wide MCP credential per poll, for the watched URL.
"""

from contextlib import asynccontextmanager

from anthropic import AsyncAnthropic
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.watch_polling import poll_endpoint
from daimon.core.config import load_settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.github_credentials import build_multifernet
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Lifespan


def create_poll_app(
    runtime: McpRuntime, *, lifespan: Lifespan[Starlette] | None = None
) -> Starlette:
    return Starlette(
        routes=[
            Route("/internal/watches/{watch_id}/poll", poll_endpoint(runtime), methods=["POST"])
        ],
        lifespan=lifespan,
    )


def create_watch_executor_app() -> Starlette:
    settings = load_settings()
    engine = build_engine(str(settings.database.url), pool_size=2, max_overflow=2)
    sessions = build_session_factory(
        engine,
        crypto_keys=tuple(key.get_secret_value() for key in settings.crypto.keys),
        allow_plaintext=settings.crypto.allow_plaintext,
    )
    client = AsyncAnthropic(api_key=settings.anthropic.resolved_api_key(), max_retries=0)
    runtime = McpRuntime(
        session_factory=sessions,
        client=client,
        settings=settings,
        deployment_default=parse_deployment_default(settings.defaults_root),
        fernet=build_multifernet(tuple(key.get_secret_value() for key in settings.crypto.keys))
        if settings.crypto.keys
        else None,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        try:
            yield
        finally:
            await client.close()
            await engine.dispose()

    return create_poll_app(runtime, lifespan=lifespan)
