"""Shared database preflight, discovered through the pytest11 entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from daimon.testing.db import DatabasePreflight


def _schema_cache_key(request: pytest.FixtureRequest) -> object:
    """Resolve the same schema dependency as the requesting fixture."""
    return request.getfixturevalue("db_schema")


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef[object]) -> None:
    """Key only our admission fixture by its resolved schema, before setup.

    Conftests re-export session schemas as distinct fixture definitions. The
    default cache key (an indirect parameter, normally None) ignores that
    change. Use FixtureDef's cache-key method for admission only: engines
    retain their normal lifetimes, dynamic fixture requests work identically,
    and database-free collection neither reads a DSN nor imports models.
    """
    if (
        fixturedef.argname == "db_preflight"
        and fixturedef.func.__module__ == __name__
        and fixturedef.func.__name__ == "db_preflight"
    ):
        fixturedef.cache_key = _schema_cache_key


@pytest.fixture(scope="session")
def db_preflight(db_schema: str) -> DatabasePreflight:
    """Admit the configured test database before any shared engine fixture runs.

    Import lazily so database-free tests do not load models or defer name writes.
    Existing conftests importing shared DB fixtures need no additional imports.
    """
    from daimon.testing.db import preflight_test_database

    return preflight_test_database(db_schema)
