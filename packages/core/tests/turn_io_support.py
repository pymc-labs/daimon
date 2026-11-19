"""Prove a test drove its turn through mux turn I/O.

The fixtures are re-exported by `conftest.py`. `turn_io_used` records which
`TurnIO` implementation the driver bound; `headless_identity` is the committed
tenant and account that a mux turn requires.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


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

    def assert_matches(self) -> None:
        """Every I/O built so far is mux turn I/O."""
        from daimon.core.turn.io import MuxTurnIO

        wrong = [cls.__name__ for cls in self.classes if not issubclass(cls, MuxTurnIO)]
        assert not wrong, f"turn used {wrong}, expected MuxTurnIO"

    def assert_ran(self) -> None:
        """At least one I/O was built, and every one is mux turn I/O."""
        assert self.classes, "no turn I/O was built; the turn never ran"
        self.assert_matches()


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
