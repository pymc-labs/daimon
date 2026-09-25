"""Persist initial turn status-card intent before platform posts.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_turn_card_intents"
down_revision: str | None = "0026_gh_install_reconcile"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "turn_card_intents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("turn_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'prepared'"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'posted', 'retired')",
            name="ck_turn_card_intents_status",
        ),
        sa.CheckConstraint(
            "(status = 'prepared' AND message_id IS NULL) OR "
            "(status = 'posted' AND message_id IS NOT NULL AND message_id <> '') OR "
            "(status = 'retired' AND (message_id IS NULL OR message_id <> ''))",
            name="ck_turn_card_intents_message_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "turn_token",
            name="uq_turn_card_intents_tenant_token",
        ),
    )
    op.create_index(
        "ix_turn_card_intents_recovery",
        "turn_card_intents",
        ["platform", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_turn_card_intents_recovery", table_name="turn_card_intents")
    op.drop_table("turn_card_intents")
