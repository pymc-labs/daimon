"""Drop `channel_admins`, the per-channel admin grants nothing writes any more.

Every channel write is a workspace admin's, so the table only held rows from before that
rule. The privacy preview and the account purge no longer read it. Downgrading recreates
the table empty.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0174_drop_channel_admins"
down_revision: str | None = "0172_channel_settings"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("channel_admins")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "channel_admins",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column(
            "role_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "user_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("updated_by_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "platform", "channel_id", name="pk_channel_admins"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_channel_admins_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_channel_admins_updated_by_account_id",
        ),
        sa.CheckConstraint(
            "platform IN ('discord', 'slack', 'teams')", name="ck_channel_admins_platform"
        ),
    )
