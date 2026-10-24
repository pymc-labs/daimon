"""progress_plan_scopes

Progress plans: explicit per-scope modes (workspace, channel, thread), the
same cascade as thread participation. Carries tenant_id and cascades on
tenant teardown. Empty until someone turns a scope on, so the migration is a
no-op for every deployment that does not use the feature.

Revision ID: 0115_progress_plan_scopes
Revises: 0114_selfhosted_skill_pins

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0115_progress_plan_scopes"
down_revision: str | None = "0114_selfhosted_skill_pins"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "progress_plan_scopes",
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "platform", "scope", "scope_id", name="pk_progress_plan_scopes"
        ),
        sa.CheckConstraint(
            "scope IN ('workspace', 'channel', 'thread')", name="ck_progress_plan_scopes_scope"
        ),
        sa.CheckConstraint(
            "mode IN ('on', 'off', 'disabled')", name="ck_progress_plan_scopes_mode"
        ),
    )


def downgrade() -> None:
    op.drop_table("progress_plan_scopes")
