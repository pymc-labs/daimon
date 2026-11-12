"""neutral catalog: a shared thread's agent copy belongs to the thread, not a person.

A shared thread's owner is derived from the thread and has no account row, so the
catalog's account foreign keys come off; deleting an account deletes its rows in
code (`neutral_catalog.delete_account_rows`). Downgrading deletes rows owned by
no account, then restores the keys.

downgrade: destructive
"""

from alembic import op

revision: str = "0204_shared_agent_copies"
down_revision: str | None = "0203_pending_carry_overs"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("neutral_agent_revisions", "neutral_skill_versions")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in _TABLES:
        op.drop_constraint(f"{table}_account_id_fkey", table, type_="foreignkey")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in _TABLES:
        op.execute(
            f"DELETE FROM {table} AS t"
            " WHERE NOT EXISTS (SELECT 1 FROM accounts AS a WHERE a.id = t.account_id)"
        )
        op.create_foreign_key(
            f"{table}_account_id_fkey",
            table,
            "accounts",
            ["account_id"],
            ["id"],
            ondelete="CASCADE",
        )
