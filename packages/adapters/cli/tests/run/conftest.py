"""Fixtures for the `daimon run` tests: a real runtime over the test database.

The CLI opens the database to resolve the operator identity, so these tests
build a real `CliRuntime` over the Postgres session factory instead of bare
placeholder objects.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.turn import driver
from daimon.core.turn.io import MuxTurnIO, TurnIO, turn_io
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LOCAL_USER = "operator"

RuntimeFactory = Callable[..., CliRuntime]


@pytest.fixture
def make_rt(db_session_factory: async_sessionmaker[AsyncSession]) -> RuntimeFactory:
    """A real runtime over the test database; `client` defaults to one no request can reach."""

    def build(client: AsyncAnthropic | None = None) -> CliRuntime:
        return CliRuntime(
            settings=Settings.model_validate(
                {
                    "database": {"url": "postgresql+asyncpg://localhost/offline"},
                    "anthropic": {"api_key": "offline"},
                    "cli": {"local_user": LOCAL_USER},
                }
            ),
            anthropic=client or build_fake_anthropic(MARouter().dispatch),
            sessionmaker=db_session_factory,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
        )

    return build


@pytest.fixture
def turn_ios(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[TurnIO]]:
    """Every turn I/O the driver builds, in order."""
    built: list[TurnIO] = []

    def spy(*args: object, **kwargs: object) -> TurnIO:
        io = turn_io(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        built.append(io)
        return io

    monkeypatch.setattr(driver, "turn_io", spy)
    yield built


def assert_io_ran(built: list[TurnIO]) -> None:
    """Exactly one turn I/O was built, and it is mux turn I/O."""
    assert len(built) == 1
    assert isinstance(built[0], MuxTurnIO), f"built {type(built[0]).__name__}"


async def cli_principal_count(db: AsyncSession) -> int:
    """How many CLI principals exist."""
    return int(await db.scalar(text("SELECT count(*) FROM cli_principals")) or 0)
