"""Shared database preflight, discovered through the pytest11 entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

if TYPE_CHECKING:
    from daimon.testing.db import DatabasePreflight


def use_plain_console_tracebacks() -> None:
    """Render exceptions in structlog's unconfigured console output as plain text.

    Without a configured chain, `log.exception(...)` goes through structlog's default
    `ConsoleRenderer`, which formats the traceback with `rich` and syntax-highlights every
    frame's source through pygments: about a second of CPU per logged exception, and many
    tests exercise a failure path that logs one. The default processors are shared with
    `structlog.reset_defaults()`, so switching the formatter on that instance also holds
    after a test resets structlog. Which exceptions are logged, and the event fields that
    carry them, do not change; only the traceback's text layout does.
    """
    for processor in structlog.get_config()["processors"]:
        if isinstance(processor, structlog.dev.ConsoleRenderer):
            processor.exception_formatter = structlog.dev.plain_traceback


def pytest_configure() -> None:
    use_plain_console_tracebacks()


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
