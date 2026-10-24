"""Shared fixtures for the adapter parity suite.

Re-exports the standard per-worker-schema DB fixtures from `daimon.testing.db`
(the established pattern — no local Base import) and the shared turn router
(`daimon.testing.turn_router`) every driver uses to
fake the MA transport for a turn (agent/environment resolution + SSE event
stream). The platform entry points themselves stay in their own driver
modules.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.db import db_session as db_session
from daimon.testing.db import db_session_factory as db_session_factory
from daimon.testing.turn_router import (
    AGENT_ID,
    AGENT_TEXT,
    ENV_ID,
    MODEL_ID,
    build_turn_router,
    turn_events,
)

from .drivers.discord_driver import DiscordDriver
from .drivers.protocol import PlatformDriver
from .drivers.slack_driver import SlackDriver
from .drivers.teams_driver import TeamsDriver

if TYPE_CHECKING:
    from daimon.testing.qa_mcp_host import SharedQaHost

__all__ = ["build_turn_router", "turn_events", "AGENT_TEXT", "AGENT_ID", "ENV_ID", "MODEL_ID"]


@pytest.fixture(params=["discord", "slack", "teams"])
def driver(request: pytest.FixtureRequest) -> PlatformDriver:
    """Yield the PlatformDriver under test, parametrized over every chat platform.

    A scenario that cannot apply to one platform narrows this with
    `@pytest.mark.parametrize("driver", [...], indirect=True)`.
    """
    if request.param == "discord":
        return DiscordDriver()
    if request.param == "slack":
        return SlackDriver()
    return TeamsDriver()


@pytest.fixture(autouse=True)
def _instant_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Keep the Anthropic SDK's retries but not its sleeps.

    `build_fake_anthropic` leaves `max_retries=2`, so a faked 5xx or 429 really
    waited 0.4-1 s per retry. Retries still happen and still count; only the
    backoff is zero.
    """
    from anthropic._base_client import BaseClient

    def no_wait(self: object, *args: object, **kwargs: object) -> float:
        return 0.0

    monkeypatch.setattr(BaseClient, "_calculate_retry_timeout", no_wait)


@pytest.fixture(scope="session")
async def qa_shared_host(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[SharedQaHost]:
    """One QA host schema, tenant and account per xdist worker.

    Building the `qa_mcp_<run>` schema costs ~1 s and dropping it ~0.5 s, so
    tests take a run on it (`build_run`) instead of building their own host.
    A test that asserts cleanup drops the schema builds its own with `build_host`.
    """
    from daimon.testing.qa_mcp_host import build_shared_host, cleanup

    database_url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    assert database_url, "the parity lane must provide its own isolated database"
    root = tmp_path_factory.mktemp("qa-shared-host")
    shared = await build_shared_host(database_url=database_url, port=0, root=root)
    try:
        yield shared
    finally:
        await shared.engine.dispose()
        await cleanup(shared.manifest.path(root), database_url=database_url)


_RUNNER_PATH = Path(__file__).resolve().parents[1] / "golden" / "runner.py"


@pytest.fixture(scope="session")
def golden_replays(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Every golden scenario, replayed once per turn path and shared by all xdist workers."""
    spec = importlib.util.spec_from_file_location("oracle_runner", _RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    base = tmp_path_factory.getbasetemp()
    shared = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    replays = runner.SharedReplays(shared / "golden-replays")
    replays.runner = runner
    return replays
