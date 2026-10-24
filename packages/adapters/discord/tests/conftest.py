"""Shared fixtures for Discord adapter tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

import pytest
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_committing_engine as db_committing_engine
from daimon.testing.db import db_committing_pool as db_committing_pool
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.db import db_session as db_session
from daimon.testing.db import db_session_factory as db_session_factory
from daimon.testing.factories import make_tenant as make_tenant

_xdist_enabled = False
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


@pytest.fixture(autouse=True)
def isolate_card_edits() -> Iterator[None]:
    """Card edits finish in the background; one test's stalled edit must not
    hold the next test's edit to a card with the same id."""
    from daimon.core.posted_controls.lifecycle import cancel_pending_card_edits

    yield
    cancel_pending_card_edits()


@pytest.fixture(autouse=True)
def isolate_agent_faces() -> Iterator[None]:
    """Creating an agent starts its face render in the background; one test's
    render must not write into the next test's database or back-off state."""
    from daimon.core.agent_identity import cancel_pending_agent_faces

    yield
    cancel_pending_agent_faces()


@pytest.fixture(autouse=True)
def _status_log_off() -> Iterator[None]:
    """Pin `turn.status_log` off, so the environment cannot change what a test sees."""
    from daimon.core.turn.status_log import status_log_override

    with status_log_override(False):
        yield


@pytest.fixture
def status_log_on(_status_log_off: None) -> Iterator[None]:
    """Run a test with `turn.status_log` on; every other test runs with it off."""
    from daimon.core.turn.status_log import status_log_override

    with status_log_override(True):
        yield
