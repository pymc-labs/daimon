"""Rename imported routine identity to source identity without changing links.

downgrade: safe
"""

from alembic import op

revision = "0161_routine_source_id"
down_revision = "0158_watch_mcp_claim"
branch_labels = None
depends_on = None

LEGACY_COLUMN = "internal_routine_id"
LEGACY_CONSTRAINT = "uq_routines_tenant_internal_id"
SOURCE_CONSTRAINT = "uq_routines_tenant_source_id"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.alter_column("routines", LEGACY_COLUMN, new_column_name="source_routine_id")
    # PostgreSQL renames the backing unique index with its constraint.
    op.execute(f"ALTER TABLE routines RENAME CONSTRAINT {LEGACY_CONSTRAINT} TO {SOURCE_CONSTRAINT}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"ALTER TABLE routines RENAME CONSTRAINT {SOURCE_CONSTRAINT} TO {LEGACY_CONSTRAINT}")
    op.alter_column("routines", "source_routine_id", new_column_name=LEGACY_COLUMN)
