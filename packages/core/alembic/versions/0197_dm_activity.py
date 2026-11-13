"""date private conversations by their last activity

A private (DM) conversation kept its context until the person removed it. It now carries an
`updated_at` the stores set on every start, claimed message and finished reply, so the
`conversation_retention` sweep can age it with other conversations. Existing rows start
their clock at the migration.

Revision ID: 0197_dm_activity
Revises: 0196_audit_clocks

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0197_dm_activity"
down_revision: str | None = "0196_audit_clocks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_dm_conversations_tenant_updated"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE direct_message_conversations "
        "ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()"
    )
    with op.get_context().autocommit_block():
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)"),
            {"n": INDEX},
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            "ON direct_message_conversations (tenant_id, updated_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
    op.execute("ALTER TABLE direct_message_conversations DROP COLUMN IF EXISTS updated_at")
