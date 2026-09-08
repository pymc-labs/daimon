"""Back the hub login proxies with a Postgres key-value table so logins
survive restarts and replicas.

Revision ID: 0013_hub_oauth_kv
Revises: 0012_support_escalations
Create Date: 2026-09-06

downgrade: safe

The column set mirrors what py-key-value-aio's PostgreSQLStore creates for
itself, because that store reads and writes the table directly. It is created
here rather than by the store's auto_create so the schema stays under Alembic
like every other table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_hub_oauth_kv"
down_revision: str | None = "0012_support_escalations"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "hub_oauth_kv",
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("ttl", sa.Double(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("collection", "key"),
    )
    op.create_index("ix_hub_oauth_kv_expires_at", "hub_oauth_kv", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_hub_oauth_kv_expires_at", table_name="hub_oauth_kv")
    op.drop_table("hub_oauth_kv")
