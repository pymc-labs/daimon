"""Retain trusted attendance across model-created handoffs.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0137_turn_attendance"
down_revision: str | None = "0136_unattended_turn_provenance"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in ("turn_origins", "task_continuations"):
        op.add_column(
            table, sa.Column("attended", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    op.execute(
        "UPDATE task_continuations SET attended = true "
        "WHERE reason IN ('private_input_applied', 'github_access_ready')"
    )


def downgrade() -> None:
    for table in ("task_continuations", "turn_origins"):
        op.drop_column(table, "attended")
