"""Which turn path a platform turn really took, per CI leg.

A turn dispatched through the REAL platform entry point (the same drivers every
parity scenario uses) reaches `daimon.core.turn.run._turn_port_kwargs`, which
decides whether `run_turn` is called on the legacy or the mux I/O path. These
tests spy on that function and assert its answer, so a leg that silently runs
the other path fails here instead of passing for the wrong reason.

* `test_turn_runs_on_the_selected_leg` follows the leg CI selected with
  `DAIMON_TURN__PATH` / `DAIMON_TURN__CHANNEL_BACKENDS` (legacy when unset).
* `test_turn_runs_on_the_pinned_path` sets the environment itself, so both
  paths are asserted on every leg.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, cast

import pytest
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import Platform
from daimon.core.turn import run as turn_run
from daimon.testing.factories import make_tenant
from daimon.testing.turn_leg import turn_leg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import build_turn_router
from .drivers.protocol import PlatformDriver, platform_ids


@pytest.fixture
def port_kwargs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `_turn_port_kwargs` answer produced while the test runs."""
    seen: list[dict[str, Any]] = []
    real = turn_run._turn_port_kwargs  # pyright: ignore[reportPrivateUsage]

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        answer = cast("dict[str, Any]", real(*args, **kwargs))
        seen.append(answer)
        return answer

    monkeypatch.setattr(turn_run, "_turn_port_kwargs", spy)
    return seen


async def _dispatch_turn(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900001001, user=555000111, channel=100000
    )
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()
    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=build_turn_router(str(tenant.id)),
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        text="hello",
    )
    assert posted, "the turn posted no reply"


def _path_taken(answer: dict[str, Any]) -> str:
    # The legacy answer for an Anthropic turn omits `path`: run_turn's default.
    return cast(str, answer.get("path", "legacy"))


async def test_turn_runs_on_the_selected_leg(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    port_kwargs: list[dict[str, Any]],
) -> None:
    await _dispatch_turn(driver, db_session, db_session_factory)

    assert port_kwargs, "the turn never asked for its I/O path"
    assert {_path_taken(answer) for answer in port_kwargs} == {turn_leg().path}


@pytest.mark.parametrize("path", ["legacy", "mux"])
async def test_turn_runs_on_the_pinned_path(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    port_kwargs: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    path: Literal["legacy", "mux"],
) -> None:
    monkeypatch.setenv("DAIMON_TURN__PATH", path)
    monkeypatch.delenv("DAIMON_TURN__CHANNEL_BACKENDS", raising=False)

    await _dispatch_turn(driver, db_session, db_session_factory)

    assert port_kwargs, "the turn never asked for its I/O path"
    assert {_path_taken(answer) for answer in port_kwargs} == {path}
