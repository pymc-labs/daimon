"""Spaces: channels sharing one audience and one rule.

`spaces` holds each Space's rule; `space_channels` its channels, one Space per
channel. The Space rule is also written onto each member channel's own rule,
so a build without these tables still keeps every member contained.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0093_spaces"
down_revision: str | None = "0092_routine_post_source"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "spaces",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("readers", sa.Text(), nullable=False),
        sa.Column("writers", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_spaces_tenant_name"),
        sa.CheckConstraint(
            "(readers = 'own' AND writers IN ('own', 'none'))"
            " OR (readers = 'inside' AND writers IN ('any', 'none'))",
            name="ck_spaces_rule",
        ),
    )
    op.create_table(
        "space_channels",
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_space_channels"),
    )
    op.create_index("ix_space_channels_space_id", "space_channels", ["space_id"])


def downgrade() -> None:
    # Members keep the Space rule written onto their channel rules, so they
    # stay contained; agents naming several of them lose their shared home.
    op.drop_table("space_channels")
    op.drop_table("spaces")
