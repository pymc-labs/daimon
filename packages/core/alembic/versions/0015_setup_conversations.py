"""Setup conversation routing and trusted control origins.

Revision ID: 0015_setup_conversations
Revises: 0014_thread_participation

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0015_setup_conversations"
down_revision: str | None = "0014_thread_participation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("thread_sessions", sa.Column("ma_agent_id", sa.Text(), nullable=True))
    for field in ("platform", "parent_channel_id", "origin_thread_id", "posted_message_id"):
        op.add_column("credential_requests", sa.Column(field, sa.Text(), nullable=True))
    op.create_table(
        "thread_agent_bindings",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("parent_channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="setup"),
        sa.Column("responder_ma_agent_id", sa.Text(), nullable=False),
        sa.Column("responder_name", sa.Text(), nullable=False),
        sa.Column("configuration_target_ma_agent_id", sa.Text(), nullable=True),
        sa.Column("configuration_target_name", sa.Text(), nullable=True),
        sa.Column(
            "creator_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "platform",
            "parent_channel_id",
            "thread_id",
            name="uq_thread_agent_bindings_location",
        ),
        sa.CheckConstraint("kind = 'setup'", name="ck_thread_agent_bindings_kind"),
    )
    op.create_table(
        "turn_origins",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("parent_channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("responder_ma_agent_id", sa.Text(), nullable=False),
        sa.Column("responder_name", sa.Text(), nullable=False),
        sa.Column("configuration_target_ma_agent_id", sa.Text(), nullable=True),
        sa.Column("configuration_target_name", sa.Text(), nullable=True),
        sa.Column("is_setup", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("turn_origins_expiry_idx", "turn_origins", ["expires_at"])


def downgrade() -> None:
    op.drop_index("turn_origins_expiry_idx", table_name="turn_origins")
    op.drop_table("turn_origins")
    op.drop_table("thread_agent_bindings")
    for field in ("posted_message_id", "origin_thread_id", "parent_channel_id", "platform"):
        op.drop_column("credential_requests", field)
    op.drop_column("thread_sessions", "ma_agent_id")
