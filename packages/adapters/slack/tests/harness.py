"""Shared construction for Slack adapter tests: a `SlackRuntime` over the
test DB and a fake MA transport, and a `SlackApp` wired for orchestration
tests that drive `_orchestrate` / `_run_thread_turn`.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import httpx
from anthropic import AsyncAnthropic
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.deps import build_turn_deps
from daimon.testing import build_fake_anthropic, make_agent_env_echo_handler, make_fake_ma_handler
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def build_slack_runtime(
    fernet_key: str,
    db_factory: async_sessionmaker[AsyncSession],
    *,
    anthropic: AsyncAnthropic | None = None,
    settings: MagicMock | None = None,
) -> SlackRuntime:
    """A `SlackRuntime` for handlers that never enter the turn path.

    `settings` is a `MagicMock` (a fresh one unless the caller passes its own
    pre-configured mock) that gets the crypto key; the resolver cache and turn
    deps are stubs. `anthropic` defaults to a fake backed by
    `make_fake_ma_handler`.
    """
    if settings is None:
        settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    return SlackRuntime(
        settings=settings,
        anthropic=anthropic
        if anthropic is not None
        else build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=db_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


def make_orchestrate_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    max_concurrent_turns_per_tenant: int = 3,
    deployment_default: DeploymentDefault | None = None,
    crypto_key: str | None = None,
    connect_nudge_url: str | None = None,
) -> tuple[SlackApp, AsyncAnthropic]:
    """Build a SlackApp for orchestration tests.

    Returns (app, anthropic_client). The anthropic client is a real
    AsyncAnthropic backed by `make_agent_env_echo_handler`, which answers the
    agent / environment retrieves `_run_thread_turn` makes when creating a
    session, and the session retrieve a mapping row without a recorded
    configuration triggers.

    ``deployment_default`` defaults to the seeded defaults/config.yaml values
    (agent "daimon", environment "default") so tests without scoped rows
    resolve the same tags a fresh deployment would.

    ``crypto_key`` is None by default; pass one when the test also drives
    ``_handle_app_mention``, whose per-event token decrypt needs a real key.

    ``connect_nudge_url`` is None by default, which short-circuits
    ``_maybe_post_connect_nudge``; pass the app root URL to run the nudge
    path (a real signing secret is set alongside it).
    """
    settings = MagicMock()
    settings.crypto.keys = (SecretStr(crypto_key),) if crypto_key is not None else ()
    settings.slack.max_concurrent_turns_per_tenant = max_concurrent_turns_per_tenant
    settings.slack.history_page_limit = 100
    settings.slack.bot_display_name = "daimon"
    settings.mcp.public_url = None
    settings.mcp.app_root_url = connect_nudge_url
    if connect_nudge_url is not None:
        settings.slack.signing_secret = SecretStr("test-signing-secret")
    settings.defaults_root = MagicMock()
    settings.billing.markup = Decimal("1.0")

    anthropic_client = build_fake_anthropic(make_agent_env_echo_handler())
    resolved_deployment_default = (
        deployment_default
        if deployment_default is not None
        else DeploymentDefault(agent_name="daimon", environment_name="default")
    )
    resolver_cache = new_resolver_cache()
    turn_deps = build_turn_deps(
        settings,
        anthropic_client,
        sessionmaker,
        deployment_default=resolved_deployment_default,
        resolver_cache=resolver_cache,
        billing_config=None,
    )

    runtime = SlackRuntime(
        settings=settings,
        anthropic=anthropic_client,
        sessionmaker=sessionmaker,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=resolver_cache,
        turn_deps=turn_deps,
        deployment_default=resolved_deployment_default,
    )
    return SlackApp(runtime=runtime), anthropic_client
