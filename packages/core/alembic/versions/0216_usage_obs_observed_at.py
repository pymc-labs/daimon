"""usage_obs_observed_at: index today's usage by provider and observed time.

The provider daily ceiling summed today's usage by reading every observation's
JSON. The provider and observed time are now columns, backfilled from that JSON,
with an index built concurrently so turns keep writing usage meanwhile. A row
written by older code leaves them NULL; the readers fall back to its JSON.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0216_usage_obs_observed_at"
down_revision: str | None = "0215_gemini_records"
branch_labels: str | None = None
depends_on: str | None = None

INDEX = "ix_usage_observation_provider_observed_at"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("usage_observation", sa.Column("provider", sa.Text(), nullable=True))
    op.add_column(
        "usage_observation", sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        """UPDATE usage_observation
        SET provider = applied->'observation'->'session'->>'provider',
            observed_at = (applied->'observation'->>'observed_at')::timestamptz
        WHERE provider IS NULL"""
    )
    with op.get_context().autocommit_block():
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": INDEX},
        )
        if invalid:
            op.drop_index(INDEX, table_name="usage_observation", postgresql_concurrently=True)
        op.create_index(
            INDEX,
            "usage_observation",
            ["provider", "observed_at"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX, table_name="usage_observation", if_exists=True, postgresql_concurrently=True
        )
    op.drop_column("usage_observation", "observed_at")
    op.drop_column("usage_observation", "provider")
