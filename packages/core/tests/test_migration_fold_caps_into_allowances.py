"""Folding caps into allowances keeps the stricter limit and leaves other tenants alone."""

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path
from types import ModuleType

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


async def _rows(db: AsyncSession, tenant: uuid.UUID) -> list[tuple[str, str, Decimal, str]]:
    result = await db.execute(
        text(
            'SELECT subject_kind, subject_id, amount_usd, "window" FROM spend_allowances '
            "WHERE tenant_id = :t ORDER BY 1, 2, 4, 3"
        ),
        {"t": tenant},
    )
    return [tuple(r) for r in result.tuples()]  # type: ignore[misc]


async def test_caps_become_monthly_allowances_and_the_stricter_limit_wins(
    db_session: AsyncSession,
) -> None:
    plain = (await make_tenant(db_session)).id
    merged = (await make_tenant(db_session)).id
    held = (await make_tenant(db_session)).id
    bystander = (await make_tenant(db_session)).id
    await _cap(db_session, plain, None, "10.00")
    await _cap(db_session, plain, "u1", "40.00")
    await _cap(db_session, merged, None, "10.00")
    await _allow(db_session, merged, "group", "everyone", "20")
    await _allow(db_session, merged, "group", "everyone", "50", "total")
    await _allow(db_session, merged, "group", "staff", "3")
    await _cap(db_session, held, "u1", "40.00")
    await _allow(db_session, held, "group", "everyone", "8")
    await _allow(db_session, bystander, "group", "everyone", "99")

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            _migration().upgrade()

    await (await db_session.connection()).run_sync(upgrade)

    assert await _rows(db_session, plain) == [
        ("group", "everyone", Decimal("10.00"), "month"),
        ("person", "u1", Decimal("40.00"), "month"),
    ], "a default and an override each become a monthly allowance"
    assert await _rows(db_session, merged) == [
        ("group", "everyone", Decimal("10.00"), "month"),
        ("group", "everyone", Decimal("10.00"), "total"),
        ("group", "staff", Decimal("3.00"), "month"),
    ], "existing allowances of the same group drop to the cap; no second one is added"
    assert await _rows(db_session, held) == [
        ("group", "everyone", Decimal("8.00"), "month"),
        ("person", "u1", Decimal("8.00"), "month"),
    ], "a person's override is held to the group allowance it would otherwise replace"
    assert await _rows(db_session, bystander) == [("group", "everyone", Decimal("99.00"), "month")]
