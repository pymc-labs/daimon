"""Secrets one person shares with another, revealed once in a DM.

`secret_shares.content` is Fernet ciphertext, NULL once revealed or expired; the row stays
as the audit record. Additive only.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0222_secret_shares"
down_revision: str | None = "0221_routine_delivery_hold"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "secret_shares",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("sender_platform_user_id", sa.Text(), nullable=False),
        sa.Column("recipient_platform_user_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("awaiting_sender", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "secret_shares_sender_idx",
        "secret_shares",
        ["tenant_id", "platform", "sender_platform_user_id"],
    )
    op.create_index(
        "secret_shares_recipient_idx",
        "secret_shares",
        ["tenant_id", "platform", "recipient_platform_user_id"],
    )
    op.create_index(
        "secret_shares_live_idx",
        "secret_shares",
        ["expires_at"],
        postgresql_where=sa.text("content IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("secret_shares")
