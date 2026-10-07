"""Store per-agent PNG avatars with opaque public URLs.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053_agent_avatars"
down_revision: str | None = "0052_message_feedback_reasons"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("security_audit_events", sa.Column("agent_name", sa.Text(), nullable=True))
    op.create_table(
        "agent_avatars",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("png", sa.LargeBinary(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("updated_by_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("tenant_id", "agent_name"),
        sa.UniqueConstraint("token"),
        sa.CheckConstraint("source IN ('default', 'upload')", name="ck_agent_avatars_source"),
    )


def downgrade() -> None:
    op.drop_table("agent_avatars")
    op.drop_column("security_audit_events", "agent_name")
