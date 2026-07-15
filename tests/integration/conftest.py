"""Shared fixtures for top-level integration tests."""

from __future__ import annotations

import pytest
from daimon.testing.db import db_engine as db_engine  # noqa: F401
from daimon.testing.db import db_session as db_session  # noqa: F401
from daimon.testing.db import db_session_factory as db_session_factory  # noqa: F401


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
