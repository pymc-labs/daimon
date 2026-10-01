"""Shared fixtures for top-level integration tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from daimon.core.db import build_session_factory
from daimon.core.stores import tenant_ledger
from daimon.testing.db import build_test_engine
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.db import db_session as db_session
from daimon.testing.db import db_session_factory as db_session_factory
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def _schema_engine_test_dsn() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for integration tests")
    return url


@pytest_asyncio.fixture
async def schema_engine(
    db_schema: str,
    db_clean: None,  # orders the seed after the per-test wipe
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID]]:
    """Own pooled engine on the worker schema + sessionmaker, seeded with a funded tenant.

    Used by the routines end-to-end suite, whose scheduler ``run`` is invoked
    with this engine via ``_engine_override`` so it shares the schema with the
    test body's seed/verify code. It is a separate pooled engine (not
    ``db_engine``) because the scheduler holds an advisory-lock connection
    plus sessions at once. Every connection is pinned to the worker schema,
    which ``db_clean`` has just wiped.
    """
    dsn = _schema_engine_test_dsn()
    engine = build_test_engine(dsn, db_schema, pool_pre_ping=True)
    sm = build_session_factory(engine)

    # Seed a Tenant — the routine row's tenant_id FK points at it.
    # Seed a positive balance so the admission gate (is_over_balance)
    # does not block scheduled fires — these e2e tests exercise the fire path, not the gate.
    async with sm() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="e2e-guild-a")
        tenant_id = tenant.id
        await tenant_ledger.insert_entry(
            s,
            tenant_id=tenant_id,
            delta_usd=Decimal("100"),
            reason="test-seed",
            idempotency_key=f"test-seed:{tenant_id}",
        )

    try:
        yield engine, sm, tenant_id
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _ephemeral_scheduler_health_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the scheduler liveness responder to an OS-assigned free port.

    Integration tests that boot the real scheduler ``run()`` start its liveness
    responder, which binds ``DAIMON_SCHEDULER__HEALTH_PORT`` (default 8082). The
    default is a fixed port, so under pytest-xdist (``-n auto``) two worker
    processes running scheduler tests at once collide with EADDRINUSE. Port 0
    lets the OS assign a distinct free port per worker, keeping the production
    liveness path exercised while staying parallel-safe.
    """
    monkeypatch.setenv("DAIMON_SCHEDULER__HEALTH_PORT", "0")
