"""Temporary N7 fixture preflight until the shared N8 preflight is merged."""

from __future__ import annotations

import os

import pytest
from daimon.testing.db import build_test_engine, require_owned_sessionmaker


@pytest.fixture(scope="session", autouse=True)
async def offline_db_preflight(db_schema: str) -> None:
    """Use shared engine admission before any engine fixture acquires a connection."""
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        raise ValueError("offline fixture preflight requires a configured owned database")
    engine = build_test_engine(url, db_schema)
    try:
        try:
            require_owned_sessionmaker(engine)
        except ValueError:
            if (
                engine.url.get_backend_name() == "postgresql"
                and engine.url.host in ("localhost", "127.0.0.1")
                and engine.url.database == "daimon_test"
                and not engine.url.query
                and not (
                    os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true"
                )
            ):
                pytest.skip("requires an isolated test database; plain local contributor database")
            raise
    finally:
        await engine.dispose()
