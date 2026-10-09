"""Previewed add_skill calls a person confirms with a yes as their next chat message.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0074_pending_skill_adds"
down_revision: str | None = "0073_github_removal_notice"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "pending_skill_adds",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "account_id",
            sa.UUID(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("ma_agent_id", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("preview_origin_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_origin_id", sa.UUID()),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "pending_skill_adds_lookup_idx",
        "pending_skill_adds",
        ["tenant_id", "account_id", "platform", "thread_id"],
    )
    op.create_index("pending_skill_adds_expiry_idx", "pending_skill_adds", ["expires_at"])


def downgrade() -> None:
    op.drop_table("pending_skill_adds")
