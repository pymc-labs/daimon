"""Persist deployment-owned usage sweep sessions.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0077_usage_sweep_sessions"
down_revision: str | None = "0076_github_connect_followup"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "usage_sweep_sessions",
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_swept_at", sa.DateTime(timezone=True)),
        sa.Column("remote_status", sa.Text()),
        sa.Column("unsettled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("resumable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("priority", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("terminal_reason", sa.Text()),
        sa.Column("no_progress_reads", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("checkpoint", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_table("usage_sweep_sessions")
