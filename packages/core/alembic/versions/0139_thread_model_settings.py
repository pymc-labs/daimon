"""Keep the model a conversation's people chose for it.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0139_thread_model_settings"
down_revision: str | None = "0138_origin_session_grants"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "thread_model_settings",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "tenant_id", "platform", "thread_id", name="pk_thread_model_settings"
        ),
    )


def downgrade() -> None:
    op.drop_table("thread_model_settings")
