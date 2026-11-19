"""usage_obs_observed_at: index today's usage by provider and observed time.

The provider daily ceiling summed today's usage by reading every observation's
JSON. The provider and observed time are now columns, backfilled from that JSON,
with an index built concurrently so turns keep writing usage meanwhile. The ADD
COLUMN commits alone (its exclusive lock is metadata-only and short); the backfill
then runs in keyed batches, one transaction each, so usage writes are never held
behind it. A row written by older code or not yet backfilled leaves them NULL; the
readers fall back to its JSON.

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
BATCH = 5000

# One batch past the key cursor; returns the batch's last key, or no row when done.
BACKFILL = """
WITH batch AS (
    SELECT binding_id, observation_id, revision FROM usage_observation
    WHERE (binding_id, observation_id, revision) > (:binding, :observation, :revision)
    ORDER BY binding_id, observation_id, revision
    LIMIT :size
), done AS (
    UPDATE usage_observation u
    SET provider = u.applied->'observation'->'session'->>'provider',
        observed_at = (u.applied->'observation'->>'observed_at')::timestamptz
    FROM batch b
    WHERE u.binding_id = b.binding_id AND u.observation_id = b.observation_id
        AND u.revision = b.revision AND u.provider IS NULL
)
SELECT binding_id, observation_id, revision FROM batch
ORDER BY binding_id DESC, observation_id DESC, revision DESC LIMIT 1
"""


def backfill(bind: sa.Connection, size: int = BATCH) -> None:
    """Fill the columns from the JSON, `size` rows per statement, in key order."""
    cursor = {"binding": "", "observation": "", "revision": -1}
    while last := bind.execute(sa.text(BACKFILL), {**cursor, "size": size}).first():
        cursor = {"binding": last[0], "observation": last[1], "revision": last[2]}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("usage_observation", sa.Column("provider", sa.Text(), nullable=True))
    op.add_column(
        "usage_observation", sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True)
    )
    with op.get_context().autocommit_block():  # commits the ALTER, releasing its lock
        backfill(op.get_bind())
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
