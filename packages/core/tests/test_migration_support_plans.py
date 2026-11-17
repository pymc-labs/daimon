"""0206 on prod-shaped rows: old rows become default credits; a downgrade keeps only those."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession

_OLD_INSERT = (
    "INSERT INTO support_escalations (tenant_id, account_id, platform, platform_user_id,"
    " channel_id, message_id, ma_session_id, note, delivered_at)"
    " VALUES (:t, :a, 'slack', 'U1', 'C1', :m, :s, 'n', :d)"
)


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0206_support_plans.py"
    spec = importlib.util.spec_from_file_location("migration_support_plans", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def _run(db_session: AsyncSession, step: str) -> None:
    migration = _migration()
    conn = await db_session.connection()

    def go(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, step)()

    await conn.run_sync(go)


async def _columns(db_session: AsyncSession) -> set[str]:
    rows = await db_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'support_escalations'"
        )
    )
    return set(rows.scalars())


async def _limit(db_session: AsyncSession, tenant_id: object, person: str | None) -> object:
    amounts = "NULL, 2" if person else "5, NULL"
    return (
        await db_session.execute(
            text(
                "INSERT INTO support_limits (tenant_id, scope, scope_id, platform_user_id,"
                f" shared, each_person, \"window\") VALUES (:t, 'workspace', '', :p, {amounts},"
                " 'total') RETURNING id"
            ),
            {"t": tenant_id, "p": person},
        )
    ).scalar_one()


@pytest.mark.fresh_schema
async def test_downgrade_when_limit_and_unsettled_rows_then_only_default_credits_stay(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    limit = await _limit(db_session, tenant.id, None)
    person = await _limit(db_session, tenant.id, "U1")
    rows = {
        "default": ("default", None, None, "settled", 1),
        "limit": ("limit", limit, person, "settled", 1),
        "exempt": ("exempt", None, None, "settled", 0),
        "declined": ("limit", limit, None, "declined", 0),
        "pending": ("limit", limit, person, "pending", None),
    }
    for message, (charged_to, limit_id, person_id, status, charge) in rows.items():
        await db_session.execute(
            text(
                "INSERT INTO support_escalations (tenant_id, platform, platform_user_id,"
                " channel_id, message_id, note, charged_to, limit_id, person_limit_id, status,"
                " charge, hold) VALUES (:t, 'slack', 'U1', 'C1', :m, 'n', :c, :l, :p, :s, :ch, 0)"
            ),
            {"t": tenant.id, "m": message, "c": charged_to, "l": limit_id, "p": person_id}
            | {"s": status, "ch": charge},
        )
    await db_session.execute(text("DELETE FROM support_limits WHERE id = :p"), {"p": person})
    kept = await db_session.execute(
        text("SELECT count(*) FROM support_escalations WHERE person_limit_id IS NOT NULL")
    )
    assert kept.scalar_one() == 0, "a person's limit removed unlinks their requests"

    await _run(db_session, "downgrade")
    left = (await db_session.execute(text("SELECT message_id FROM support_escalations"))).scalars()
    assert list(left) == ["default"], (
        "limit, exempt, declined and pending rows would be old default credits"
    )
    assert not {"charged_to", "limit_id", "person_limit_id", "status"} & await _columns(
        db_session
    ), "the ledger columns are gone"


@pytest.mark.fresh_schema
async def test_upgrade_when_old_rows_then_each_is_one_settled_default_credit(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    account = await make_account(db_session)
    await _run(db_session, "downgrade")
    old = [("m1", account.id, "sess", True), ("m2", None, None, False), ("m3", None, None, True)]
    for message, account_id, session_id, delivered in old:
        await db_session.execute(
            text(_OLD_INSERT),
            {"t": tenant.id, "a": account_id, "m": message, "s": session_id}
            | {"d": datetime(2026, 10, 1, tzinfo=UTC) if delivered else None},
        )

    await _run(db_session, "upgrade")
    got = await db_session.execute(
        text(
            "SELECT charged_to, limit_id, person_limit_id, hold, charge, status, option_key"
            " FROM support_escalations ORDER BY message_id"
        )
    )
    assert [tuple(r) for r in got] == [("default", None, None, 1, 1, "settled", None)] * 3, (
        "every old row, delivered or not, is one settled default credit"
    )
    spent = await db_session.execute(
        text(
            "SELECT sum(COALESCE(charge, hold)) FROM support_escalations"
            " WHERE charged_to = 'default' AND platform_user_id = 'U1'"
        )
    )
    assert spent.scalar_one() == len(old), "the default sum equals the old count"

    await db_session.execute(
        text(_OLD_INSERT), {"t": tenant.id, "a": None, "m": "m4", "s": None, "d": None}
    )
    late = await db_session.execute(
        text("SELECT charged_to, status FROM support_escalations WHERE message_id = 'm4'")
    )
    assert tuple(late.one()) == ("default", "settled"), "an older pod's insert counts as before"
    person = await _limit(db_session, tenant.id, "U1")
    await db_session.execute(
        text("UPDATE support_escalations SET person_limit_id = :p WHERE message_id = 'm1'"),
        {"p": person},
    )
    await db_session.execute(text("DELETE FROM support_limits WHERE id = :p"), {"p": person})
    linked = await db_session.execute(
        text("SELECT count(*) FROM support_escalations WHERE person_limit_id IS NOT NULL")
    )
    assert linked.scalar_one() == 0, "the person's limit is a SET NULL key"
