"""Real isolated Postgres fixtures; ingress uses the migrated SQL boundary."""

import pytest
from daimon.testing.db import (  # noqa: F401
    db_clean,
    db_engine,
    db_schema,
    db_session,
    db_session_factory,
)
from sqlalchemy import text


@pytest.fixture
async def watch_privilege_audit_lock(db_engine):  # noqa: F811
    # PUBLIC/inherited grant probes deliberately affect every sidecar login.
    async with db_engine.begin() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(813749202)"))
        yield
