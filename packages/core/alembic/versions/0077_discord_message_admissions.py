"""Persist Discord admissions and identify live turn owners.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0077_discord_message_admissions"
down_revision: str | None = "0076_github_connect_followup"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("turn_card_intents", sa.Column("owner_key", sa.BigInteger()))
    op.add_column("thread_sessions", sa.Column("active_turn_owner_key", sa.BigInteger()))
    op.create_table(
        "discord_message_admissions",
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("message_id", sa.Text(), primary_key=True),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("owner_key", sa.BigInteger(), nullable=False),
        sa.Column(
            "turn_card_intent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("turn_card_intents.id", ondelete="SET NULL"),
        ),
        sa.Column("handled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_discord_admissions_activity", "discord_message_admissions", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_column("turn_card_intents", "owner_key")
    op.drop_table("discord_message_admissions")
    op.drop_column("thread_sessions", "active_turn_owner_key")
