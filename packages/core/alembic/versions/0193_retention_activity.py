"""record conversation activity for retention; cascade slack turn contexts with the tenant

`thread_sessions.last_turn_at` and `journal_session.last_appended_at` say when a
conversation was last used, so the retention sweep can age it. Existing mappings take
their newest known turn; existing journal heads start their clock now, since nothing
recorded their last append. `slack_turn_contexts` gains its missing tenant foreign key,
after rows of tenants already deleted are dropped.

Revision ID: 0193_retention_activity
Revises: 0192_reset_allow_writes

downgrade: safe
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0193_retention_activity"
down_revision: str | None = "0192_reset_allow_writes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "thread_sessions",
        sa.Column("last_turn_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    )
    op.execute(
        "UPDATE thread_sessions SET last_turn_at = "
        "GREATEST(created_at, updated_at, active_turn_started_at)"
    )
    op.execute(
        "UPDATE thread_sessions t SET last_turn_at = GREATEST(t.last_turn_at, o.started_at) "
        "FROM (SELECT session_id, max(started_at) AS started_at FROM turn_outcomes "
        "WHERE session_id IS NOT NULL GROUP BY session_id) o "
        "WHERE o.session_id = t.ma_session_id"
    )
    op.add_column(
        "journal_session",
        sa.Column(
            "last_appended_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW
        ),
    )
    op.execute(
        "DELETE FROM slack_turn_contexts c "
        "WHERE NOT EXISTS (SELECT 1 FROM tenants t WHERE t.id = c.tenant_id)"
    )
    op.create_foreign_key(
        "fk_slack_turn_contexts_tenant",
        "slack_turn_contexts",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_slack_turn_contexts_tenant", "slack_turn_contexts", type_="foreignkey")
    op.drop_column("journal_session", "last_appended_at")
    op.drop_column("thread_sessions", "last_turn_at")
