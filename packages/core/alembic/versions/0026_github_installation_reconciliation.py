"""Queue authoritative GitHub installation repository reconciliation.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026_gh_install_reconcile"
down_revision: str | None = "0025_github_push_resync_queue"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "github_installation_reconciliations",
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
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
            "state IN ('pending', 'running', 'done')",
            name="ck_github_installation_reconciliations_state",
        ),
        sa.PrimaryKeyConstraint("installation_id"),
    )
    op.create_index(
        "ix_github_installation_reconciliations_due",
        "github_installation_reconciliations",
        ["state", "available_at", "created_at"],
    )
    op.create_table(
        "github_installation_deliveries",
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index(
        "ix_github_installation_deliveries_received_at",
        "github_installation_deliveries",
        ["received_at"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO github_installation_reconciliations
                (installation_id, generation, state, attempts, available_at)
            SELECT installation_id, 1, 'pending', 0, CURRENT_TIMESTAMP
            FROM github_app_installations
            ON CONFLICT (installation_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_github_installation_deliveries_received_at",
        table_name="github_installation_deliveries",
    )
    op.drop_table("github_installation_deliveries")
    op.drop_index(
        "ix_github_installation_reconciliations_due",
        table_name="github_installation_reconciliations",
    )
    op.drop_table("github_installation_reconciliations")
