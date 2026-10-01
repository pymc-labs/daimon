"""Opt-in private conversations and explicit workspace selection.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0030_feat001_direct_messages"
down_revision: str | None = "0029_sys050_security_audit"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "direct_message_policies",
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "direct_message_conversations",
        sa.Column("platform", sa.Text(), primary_key=True),
        sa.Column("route_key", sa.Text(), primary_key=True),
        sa.Column("external_user_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("memory_read_only", sa.Boolean(), nullable=False),
        sa.Column("history", pg.JSONB(), nullable=False),
        sa.Column("recent_message_ids", pg.ARRAY(sa.Text()), nullable=False),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("direct_message_conversations")
    op.drop_table("direct_message_policies")
