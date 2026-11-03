"""channel_changes: one row per channel setting change; channel names learn their place.

`channel_changes` is new: every surface records the channel, the setting, its before
and after values, the acting account (SET NULL on erasure) and the surface.
`platform_channel_names` gains nullable `kind`, `parent_id`, `parent_name` and
`last_seen_at`, filled as adapters see channels again. Downgrading drops the table and
the columns, and with them the change history.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0171_channel_changes"
down_revision: str | None = "0170_helpers"
branch_labels: str | None = None
depends_on: str | None = None

_NAME_COLUMNS = ("kind", "parent_id", "parent_name")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "channel_changes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("setting", sa.Text(), nullable=False),
        sa.Column("before", JSONB(), nullable=True),
        sa.Column("after", JSONB(), nullable=True),
        sa.Column("actor_account_id", UUID(as_uuid=True), nullable=True),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "surface IN ('web', 'chat', 'panel', 'cli')", name="ck_channel_changes_surface"
        ),
    )
    op.create_index(
        "ix_channel_changes_tenant_channel_at", "channel_changes", ["tenant_id", "channel_id", "at"]
    )
    for column in _NAME_COLUMNS:
        op.add_column("platform_channel_names", sa.Column(column, sa.Text(), nullable=True))
    op.add_column(
        "platform_channel_names",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for column in (*_NAME_COLUMNS, "last_seen_at"):
        op.drop_column("platform_channel_names", column)
    op.drop_index("ix_channel_changes_tenant_channel_at", table_name="channel_changes")
    op.drop_table("channel_changes")
