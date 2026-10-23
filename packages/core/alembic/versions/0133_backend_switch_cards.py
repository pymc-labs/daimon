"""backend_switch_cards: a provider switch waiting on its requester's Confirm card.

A turn on a provider with no confirmation surface of its own (OpenAI, Gemini)
asks Daimon to post the card itself. The row holds what the card confirms and
where; the click re-verifies everything before applying. Additive only;
downgrading drops the table, and unanswered cards then do nothing when pressed.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0133_backend_switch_cards"
down_revision: str | None = "0128_fleet_run_end_notice"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "backend_switch_cards",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("setup_thread", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requester_platform_user_id", sa.Text(), nullable=False),
        sa.Column("answering_agent_name", sa.Text(), nullable=True),
        sa.Column("reference", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'applied', 'refused', 'cancelled')",
            name="ck_backend_switch_cards_status",
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("backend_switch_cards")
