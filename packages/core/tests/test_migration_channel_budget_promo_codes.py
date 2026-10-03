"""The channel budget promo code migration round-trips and drops only its own codes."""

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.promo_codes import build_promo_code_terms
from daimon.core.stores import promo_codes as promo_store
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0042_channel_budget_promo_codes.py"
    spec = importlib.util.spec_from_file_location("migration_channel_budget_promo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_downgrade_drops_channel_codes_and_upgrade_admits_them_again(
    db_session: AsyncSession,
) -> None:
    for code_hash, channel_budget in (("credit", False), ("channel", True)):
        terms = build_promo_code_terms(
            amount_usd=Decimal("3"), timed=False, channel_budget=channel_budget
        )
        await promo_store.insert_promo_code(db_session, code_hash=code_hash, terms=terms)
    migration = _migration()
    conn = await db_session.connection()

    def downgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(downgrade)
    kinds = await db_session.execute(text("SELECT kind FROM promo_codes"))
    assert kinds.scalars().all() == ["credit"], "only the channel budget code is dropped"
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO promo_codes (code_hash, amount_usd, kind) "
                    "VALUES ('x', 1, 'channel_budget')"
                )
            )

    await conn.run_sync(upgrade)
    terms = build_promo_code_terms(amount_usd=Decimal("3"), timed=False, channel_budget=True)
    assert await promo_store.insert_promo_code(db_session, code_hash="again", terms=terms)
    column = await db_session.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'promo_redemptions' AND column_name = 'channel_id'"
        )
    )
    assert column.scalar_one() == "YES", "the redemption's channel is back"
