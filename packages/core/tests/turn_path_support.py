"""Run a test on both turn paths and prove which turn I/O it used.

The fixtures are re-exported by `conftest.py`. A test asks for `turn_path`
(parametrized `legacy`/`mux`), hands it to the code under test as an argument,
and asks `turn_io_used` which `TurnIO` implementation the driver bound.
`headless_identity` is the committed tenant and account that a mux turn
requires.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

TurnPath = Literal["legacy", "mux"]


@pytest.fixture(params=["legacy", "mux"])
def turn_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> TurnPath:
    """Run the test once per turn path, both legs in the same session.

    Tests hand this to the code under test as an argument. The environment is
    pinned to the same value only so code that has not been given the path yet
    agrees with it; `DAIMON_TURN__PATH` of the surrounding run does not matter.
    """
    path: TurnPath = request.param
    monkeypatch.setenv("DAIMON_TURN__PATH", path)
    return path


@pytest.fixture
def env_turn_path() -> TurnPath:
    """The turn path the surrounding run selects (`DAIMON_TURN__PATH`), not parametrized.

    For tests the golden oracle replays (`tests/golden/runner.py` `SCENARIOS`):
    the oracle pins the path per replay and records one transcript per test,
    so those tests must run once, on that path. The CI legacy and mux legs
    still cover both paths.
    """
    from daimon.core.config import load_turn_settings

    return load_turn_settings().path


@dataclass(frozen=True)
class HeadlessIdentity:
    """A committed tenant and one of its accounts, the identity a mux turn requires."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID


@pytest.fixture
async def headless_identity(db_session: AsyncSession) -> HeadlessIdentity:
    from daimon.testing.factories import make_account, make_tenant

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    return HeadlessIdentity(tenant_id=tenant.id, account_id=account.id)


class TurnIOSpy:
    """The `TurnIO` classes the driver built for the turns of one test."""

    def __init__(self) -> None:
        self.classes: list[type] = []

    def assert_matches(self, path: TurnPath) -> None:
        """Every I/O built so far belongs to `path`'s implementation."""
        from daimon.core.turn.io import LegacyTurnIO, MuxTurnIO

        expected = LegacyTurnIO if path == "legacy" else MuxTurnIO
        wrong = [cls.__name__ for cls in self.classes if not issubclass(cls, expected)]
        assert not wrong, f"{path} turn used {wrong}, expected {expected.__name__}"

    def assert_ran(self, path: TurnPath) -> None:
        """At least one I/O was built, and every one belongs to `path`."""
        assert self.classes, f"no turn I/O was built; the {path} path never ran"
        self.assert_matches(path)


@pytest.fixture
def turn_io_used(monkeypatch: pytest.MonkeyPatch) -> TurnIOSpy:
    """Record which `TurnIO` implementation the driver binds for each turn."""
    from daimon.core.turn import driver

    spy = TurnIOSpy()
    build = driver.turn_io

    def recording(*args: Any, **kwargs: Any) -> Any:
        io = build(*args, **kwargs)
        spy.classes.append(type(io))
        return io

    monkeypatch.setattr(driver, "turn_io", recording)
    return spy
