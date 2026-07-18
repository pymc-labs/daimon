"""Conftest for CLI adapter flow tests.

Dual-gated: skip when either DAIMON_TEST_ANTHROPIC_API_KEY or
DAIMON_DATABASE__TEST_URL is missing. Tests call real MA API and real Postgres.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands import agents as agents_cmd
from daimon.adapters.cli.commands import defaults as defaults_cmd
from daimon.adapters.cli.commands import environments as environments_cmd
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core._models import Base
from daimon.core.config import Settings
from daimon.core.ma import delete_entire_workspace_for_testing
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

# Each test module in this package must set:
#   pytestmark = pytest.mark.contract
# at module level. pytestmark in conftest.py is NOT inherited by child test files.


def require_flow_prerequisites() -> tuple[str, str]:
    """Return (api_key, db_url) or skip if either is missing."""
    api_key = os.environ.get("DAIMON_TEST_ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("DAIMON_TEST_ANTHROPIC_API_KEY not set — flow tests skipped")
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL not set — flow tests skipped")
    db_name = urlparse(url).path.lstrip("/")
    if "test" not in db_name:
        raise RuntimeError(
            f"Refusing to run destructive fixtures against database {db_name!r} "
            f"(from {url!r}) — database name must contain the substring 'test'."
        )
    return api_key, url


@pytest_asyncio.fixture(scope="module")
async def anthropic_client() -> AsyncAnthropic:
    api_key, _url = require_flow_prerequisites()
    return AsyncAnthropic(api_key=api_key)


# ---- DB fixtures (schema-per-test isolation, NullPool for multi-loop safety) ----


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine]:
    _api_key, url = require_flow_prerequisites()
    # NullPool: each asyncio.run() (inside Typer commands) gets a fresh
    # asyncpg connection bound to its own event loop.
    engine = create_async_engine(url, poolclass=NullPool)

    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL AS has_alembic")
        )
        has_alembic = result.scalar_one()
        if not has_alembic:
            await engine.dispose()
            raise RuntimeError(
                "alembic_version table is missing on the test DB. Run "
                "`uv run alembic upgrade head` against "
                "DAIMON_DATABASE_URL=<test DSN> before invoking pytest."
            )

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(
    db_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh schema + sessionmaker bound to the engine (not a connection).

    NullPool on db_engine ensures each loop gets a fresh asyncpg connection.
    """
    schema = f"test_{uuid.uuid4().hex}"

    async with db_engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn2 = await conn.execution_options(schema_translate_map={None: schema})
        await conn2.run_sync(Base.metadata.create_all)
        await conn.commit()

    sessionmaker = async_sessionmaker(
        db_engine.execution_options(schema_translate_map={None: schema}),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    try:
        yield sessionmaker
    finally:
        async with db_engine.connect() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await conn.commit()


@pytest_asyncio.fixture(autouse=True)
async def seed_tenant(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        await make_tenant(session, platform="cli", workspace_id="local")
        await session.commit()


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _cleanup(anthropic_client: AsyncAnthropic) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Nuke all MA resources before and after each test module."""
    await delete_entire_workspace_for_testing(
        anthropic_client, i_understand_this_destroys_all_tenants=True
    )
    yield
    await delete_entire_workspace_for_testing(
        anthropic_client, i_understand_this_destroys_all_tenants=True
    )


# ---- Runtime injection helper ----


def _build_settings_for_flow(*, api_key: str, db_url: str) -> Settings:
    """Build a minimal Settings object for flow tests, bypassing env validation."""

    class _Cli:
        local_user = "testuser"

    class _Mcp:
        jwt_secret = None
        public_url = None

    class _Anthropic:
        def get_secret_value(self) -> str:
            return api_key

        base_url = "https://api.anthropic.com"

    class _Database:
        url = db_url

    class _FlowSettings:
        cli = _Cli()
        mcp = _Mcp()
        anthropic = _Anthropic()
        database = _Database()

    return cast(Settings, _FlowSettings())


def install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Patch build_runtime and load_settings across CLI command modules for flow tests.

    Wires the real AsyncAnthropic client (from test API key) and the test
    sessionmaker (schema-per-test DB) into every CLI command that flow tests exercise.
    """
    api_key, db_url = require_flow_prerequisites()
    settings = _build_settings_for_flow(api_key=api_key, db_url=db_url)

    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", settings)
    object.__setattr__(rt, "anthropic", anthropic)
    object.__setattr__(rt, "sessionmaker", sessionmaker)

    @asynccontextmanager
    async def fake_build_runtime(_settings: Settings) -> AsyncIterator[CliRuntime]:
        yield rt

    for mod in (agents_cmd, environments_cmd, defaults_cmd):
        monkeypatch.setattr(mod, "build_runtime", fake_build_runtime)
        monkeypatch.setattr(mod, "load_settings", lambda: settings)
