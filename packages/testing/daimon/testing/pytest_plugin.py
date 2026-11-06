"""Shared database preflight, discovered through the pytest11 entry point."""

from __future__ import annotations

import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
import structlog

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from anthropic.types.beta import FileMetadata
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


@contextmanager
def restoring_structlog_config() -> Iterator[None]:
    """Put structlog's global configuration back the way it was on exit.

    Production setup (`configure_logging`, which app factories such as `create_mcp_app`
    call) swaps in an INFO-filtering, JSON-rendering chain for the whole process. Left in
    place, it drops the debug events a later test captures with
    `structlog.testing.capture_logs()`, which replaces only the processors, so whether that
    test passes depends on what ran before it on the same worker.
    """
    was_configured = structlog.is_configured()
    saved = structlog.get_config()
    saved["processors"] = list(saved["processors"])
    saved_hooks = (sys.excepthook, threading.excepthook)
    try:
        yield
    finally:
        # `configure_logging` also installs the uncaught-exception hooks, and a
        # service's shutdown marks every later line; none of that outlives the test.
        sys.excepthook, threading.excepthook = saved_hooks
        logging_setup = sys.modules.get("daimon.core.logging_setup")
        if logging_setup is not None:
            logging_setup.clear_shutting_down()
        observability = sys.modules.get("daimon.core.observability")
        if observability is not None:
            observability.set_unraisable_logger(None)
        if not was_configured:
            structlog.reset_defaults()
        elif structlog.get_config() != saved:
            structlog.configure(**saved)


@pytest.fixture(autouse=True)
def _restore_structlog_config() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    with restoring_structlog_config():
        yield


@pytest.fixture
def instant_routine_file_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a routine fire's generated-file settle polls without waiting them out.

    The scheduler calls `collect_routine_files` with its default real `asyncio.sleep`,
    so every routine fire that reaches file collection waits the 6 s settle floor on
    empty listings. Every listing poll and the settle rule still run; only the sleep
    between polls is instant. Core's output-delivery tests prove the real schedule.
    """
    import asyncio

    from daimon.core import output_delivery, routine_files

    real_poll = output_delivery.poll_output_files

    async def no_wait(_delay: float) -> None:
        await asyncio.sleep(0)

    async def poll(
        client: AsyncAnthropic, *, session_id: str, sleep: Callable[[float], Awaitable[None]]
    ) -> dict[str, FileMetadata]:
        del sleep  # The real `asyncio.sleep` the scheduler passes by default.
        return await real_poll(client, session_id=session_id, sleep=no_wait)

    monkeypatch.setattr(routine_files, "poll_output_files", poll)


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
