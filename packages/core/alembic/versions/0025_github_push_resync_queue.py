"""Persist and coalesce verified GitHub push resync work.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_github_push_resync_queue"
down_revision: str | None = "0024_pending_payment_clawbacks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "github_push_resyncs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("claimed_generation", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'done')", name="ck_github_push_resyncs_state"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_full_name", "ref", name="uq_github_push_resyncs_repo_ref"),
    )
    op.create_index(
        "ix_github_push_resyncs_due",
        "github_push_resyncs",
        ["state", "available_at", "created_at"],
    )
    op.create_table(
        "github_push_deliveries",
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index(
        "ix_github_push_deliveries_received_at",
        "github_push_deliveries",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_github_push_deliveries_received_at", table_name="github_push_deliveries")
    op.drop_table("github_push_deliveries")
    op.drop_index("ix_github_push_resyncs_due", table_name="github_push_resyncs")
    op.drop_table("github_push_resyncs")
