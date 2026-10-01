"""thread_participation

Organic thread participation: explicit per-scope modes (workspace, channel,
thread) and the ledger of unprompted replies the hourly cap is derived from.
Both carry tenant_id and cascade on tenant teardown. Empty until someone
turns a scope on, so the migration is a no-op for every deployment that does
not use the feature.

Revision ID: 0014_thread_participation
Revises: 0013_hub_oauth_kv

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0014_thread_participation"
down_revision: str | None = "0013_hub_oauth_kv"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "thread_participation_scopes",
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
            "tenant_id", "platform", "scope", "scope_id", name="pk_thread_participation_scopes"
        ),
        sa.CheckConstraint(
            "scope IN ('workspace', 'channel', 'thread')",
            name="ck_thread_participation_scopes_scope",
        ),
        sa.CheckConstraint(
            "mode IN ('on', 'off', 'disabled')", name="ck_thread_participation_scopes_mode"
        ),
    )
    op.create_table(
        "thread_auto_responses",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "thread_auto_responses_thread_idx",
        "thread_auto_responses",
        ["tenant_id", "platform", "thread_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("thread_auto_responses_thread_idx", table_name="thread_auto_responses")
    op.drop_table("thread_auto_responses")
    op.drop_table("thread_participation_scopes")
