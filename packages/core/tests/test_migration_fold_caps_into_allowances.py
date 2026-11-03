"""Folding caps adds them as cap rows, converted by the markup, and touches no other row."""

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0171_fold_caps_into_allowances.py"
    spec = importlib.util.spec_from_file_location("migration_fold_caps_into_allowances", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _cap(db: AsyncSession, tenant: uuid.UUID, user: str | None, usd: str) -> None:
    await db.execute(
        text(
            "INSERT INTO tenant_user_caps (tenant_id, platform_user_id, cap_usd) VALUES (:t, :u, :c)"
        ),
        {"t": tenant, "u": user, "c": Decimal(usd)},
    )


async def _allow(
    db: AsyncSession, tenant: uuid.UUID, kind: str, subject: str, usd: str, window: str = "month"
) -> None:
    await db.execute(
        text(
            "INSERT INTO spend_allowances (tenant_id, subject_kind, subject_id, amount_usd, "
            "\"window\", starts_at) VALUES (:t, :k, :s, :a, :w, now() - interval '40 days')"
        ),
        {"t": tenant, "k": kind, "s": subject, "a": Decimal(usd), "w": window},
    )


async def _rows(db: AsyncSession, tenant: uuid.UUID) -> list[tuple[str, str, Decimal, str, bool]]:
    result = await db.execute(
        text(
            'SELECT subject_kind, subject_id, amount_usd, "window", is_cap FROM spend_allowances '
            "WHERE tenant_id = :t ORDER BY 1, 2, 4, 3"
        ),
        {"t": tenant},
    )
    return [tuple(r) for r in result.tuples()]  # type: ignore[misc]


async def _upgrade(db: AsyncSession) -> None:
    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            _migration().upgrade()

    await (await db.connection()).run_sync(upgrade)


async def test_caps_become_monthly_cap_rows_and_no_allowance_is_changed(
    db_session: AsyncSession,
) -> None:
    plain = (await make_tenant(db_session)).id
    beside = (await make_tenant(db_session)).id
    bystander = (await make_tenant(db_session)).id
    await _cap(db_session, plain, None, "10.00")
    await _cap(db_session, plain, "u1", "40.00")
    await _cap(db_session, beside, None, "10.00")
    await _allow(db_session, beside, "group", "everyone", "20")
    await _allow(db_session, beside, "group", "everyone", "50", "total")
    await _allow(db_session, beside, "group", "staff", "3")
    await _allow(db_session, bystander, "group", "everyone", "99")

    await _upgrade(db_session)

    assert await _rows(db_session, plain) == [
        ("group", "everyone", Decimal("10.00"), "month", True),
        ("person", "u1", Decimal("40.00"), "month", True),
    ], "a default and an override each become a monthly cap row"
    assert await _rows(db_session, beside) == [
        ("group", "everyone", Decimal("10.00"), "month", True),
        ("group", "everyone", Decimal("20.00"), "month", False),
        ("group", "everyone", Decimal("50.00"), "total", False),
        ("group", "staff", Decimal("3.00"), "month", False),
    ], "allowances beside a cap keep their amounts and windows"
    assert await _rows(db_session, bystander) == [
        ("group", "everyone", Decimal("99.00"), "month", False)
    ]


async def test_caps_are_converted_by_the_markup_so_the_effective_limit_holds(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_BILLING__MARKUP", "1.25")
    tenant = (await make_tenant(db_session)).id
    await _cap(db_session, tenant, None, "10.00")
    await _cap(db_session, tenant, "u1", "40.00")

    await _upgrade(db_session)

    assert await _rows(db_session, tenant) == [
        ("group", "everyone", Decimal("12.50"), "month", True),
        ("person", "u1", Decimal("50.00"), "month", True),
    ]
