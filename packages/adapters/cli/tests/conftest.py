"""Shared fixtures for CLI adapter tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import pytest_asyncio
import structlog
from daimon.testing.db import (  # noqa: F401  # pyright: ignore[reportUnusedImport]
    db_clean,
    db_engine,
    db_nullpool_engine,
    db_schema,
    db_session,
)
from daimon.testing.factories import make_tenant
from daimon.testing.ma import make_stub_anthropic, stub_anthropic  # noqa: F401
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def _reset_structlog_config() -> Iterator[None]:
    """Isolate structlog's global config per CLI test.

    CLI commands call `configure_admin_logging()`, which binds the *current*
    `sys.stderr` into structlog's global config via
    `PrintLoggerFactory(file=sys.stderr)`. Under pytest that `sys.stderr` is a
    per-test capture buffer; once it closes at teardown, any later structlog
    call in the session writes to a closed file ("I/O operation on closed
    file"). Resetting around every CLI test stops one test's logging config
    leaking into another's.
    """
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


@pytest_asyncio.fixture
async def db_session_factory(
    db_session: AsyncSession,  # noqa: F811  # fixture dependency; db_session is imported above for discovery
    request: pytest.FixtureRequest,
) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to `db_session`'s connection, with the `cli:local`
    tenant pre-seeded (the seed runs after `db_clean` has wiped the worker
    schema, via the `db_session -> db_clean` dependency chain).

    Since 58.4-05 `discover_tenant` is a pure read: it derives the `cli:local`
    tenant id and raises `defaults_missing` when no such row exists — it never
    INSERTs. CLI command tests bootstrap a session through that path, so the
    tenant must already exist. Seeding it here (rather than in every test) keeps
    the 48 command tests on one shared fix.

    Tests that own their tenant lifecycle (the `tenants` command tests, the
    `discover_tenant` unit tests) mark `no_cli_local_seed` to opt out and avoid
    a UNIQUE(platform, external_id) collision.
    """
    if request.node.get_closest_marker("no_cli_local_seed") is None:
        await make_tenant(db_session, platform="cli", workspace_id="local")
        await db_session.commit()
    return async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
