"""Fence Teams activity redeliveries across workers.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0079_teams_activity_claims"
down_revision: str | None = "0078_privacy_session_deletes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "teams_activity_claims",
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            primary_key=True,
        ),
        sa.Column("conversation_id", sa.Text(), primary_key=True),
        sa.Column("activity_id", sa.Text(), primary_key=True),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("outcome_id", sa.Uuid(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_teams_activity_claims_created", "teams_activity_claims", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_teams_activity_claims_created", table_name="teams_activity_claims")
    op.drop_table("teams_activity_claims")
