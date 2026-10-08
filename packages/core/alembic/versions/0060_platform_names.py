"""The last names chat platforms gave for a tenant's people and channels.

`platform_user_names` keeps, per (tenant, platform, platform user), the
display name and handle last seen on an inbound message, a click or a lookup
that succeeded; `platform_channel_names` keeps a channel's last seen name. The
billing panel falls back to them when the platform no longer answers. Both
start empty.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060_platform_names"
down_revision: str | None = "0059_security_audit_agent_name"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "platform_user_names",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_user_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("handle", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "platform", "platform_user_id", name="pk_platform_user_names"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_platform_user_names_tenants"
        ),
        sa.CheckConstraint(
            "display_name IS NOT NULL OR handle IS NOT NULL",
            name="ck_platform_user_names_some_name",
        ),
    )
    op.create_table(
        "platform_channel_names",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "platform", "channel_id", name="pk_platform_channel_names"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_platform_channel_names_tenants",
        ),
    )


def downgrade() -> None:
    op.drop_table("platform_channel_names")
    op.drop_table("platform_user_names")
