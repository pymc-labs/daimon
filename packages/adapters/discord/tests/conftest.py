"""Shared fixtures for Discord adapter tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path

import pytest
import pytest_asyncio
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_committing_engine as db_committing_engine
from daimon.testing.db import db_committing_pool as db_committing_pool
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.db import db_session as db_session
from daimon.testing.db import db_session_factory as db_session_factory
from daimon.testing.factories import make_tenant as make_tenant

_xdist_enabled = False


@pytest_asyncio.fixture(autouse=True)
async def _end_audience_renewal() -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    """A bot that got a ready or a resume renews room facts until its process ends. Tests
    share one event loop, so end that loop with the test: it must not tick and log into a
    later one."""
    yield
    loops = [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "discord.audience_renewal" and not task.done()
    ]
    for task in loops:
        task.cancel()
    await asyncio.gather(*loops, return_exceptions=True)


_xdist_worker = False


def pytest_configure(config: pytest.Config) -> None:
    global _xdist_enabled, _xdist_worker
    _xdist_worker = hasattr(config, "workerinput")
    _xdist_enabled = _xdist_worker or bool(config.getoption("numprocesses", default=0))


def _active_test_file() -> Path | None:
    """Leave the current xdist test ID for CI to collect after a hung step."""
    directory = os.environ.get("DAIMON_PYTEST_HANG_DIR")
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not directory or not worker:
        return None
    return Path(directory) / f"discord-pytest-current-{worker}.txt"


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    if path := _active_test_file():
        path.write_text(f"{nodeid}\n", encoding="utf-8")


def pytest_runtest_logfinish(nodeid: str, location: tuple[str, int | None, str]) -> None:
    if path := _active_test_file():
        path.unlink(missing_ok=True)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Persist failure details as they happen so a later timeout keeps them."""
    if not report.failed:
        return
    if _xdist_enabled and not _xdist_worker:
        # xdist forwards each worker report to the controller; write once in
        # the worker so the report includes the worker ID and is not duplicated.
        return
    directory = os.environ.get("DAIMON_PYTEST_HANG_DIR")
    if not directory:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    digest = sha256(f"{worker}\0{report.nodeid}\0{report.when}".encode()).hexdigest()[:12]
    path = Path(directory) / f"discord-pytest-failure-{worker}-{digest}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"worker: {worker}\nphase: {report.when}\nnodeid: {report.nodeid}\n\n"
        f"{report.longreprtext}\n",
        encoding="utf-8",
    )
