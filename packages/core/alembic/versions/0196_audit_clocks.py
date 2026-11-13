"""keep cancel audit and a watch's last firing past the rows they used to cascade with

A job's cancel-audit rows were deleted with the job on the job-output clock, and a watch
lost its last-fired time when its firings went with their wake. The audit row now stands
on its own and ages by `requested_at` with the other audit records; a trigger copies each
firing's time onto its watch, so the watch keeps it.

Revision ID: 0196_audit_clocks
Revises: 0195_retention_runs

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0196_audit_clocks"
down_revision: str | None = "0195_retention_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_long_job_cancel_audit_requested_at"
FK = "long_job_cancel_audit_id_fkey"

FUNCTION = """
CREATE OR REPLACE FUNCTION {schema}.note_watch_firing() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    UPDATE {schema}.watches SET last_fired_at=greatest(last_fired_at, NEW.created_at)
      WHERE id=NEW.watch_id;
    RETURN NULL;
END
$body$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    schema = bind.dialect.identifier_preparer.quote_schema(
        bind.scalar(sa.text("SELECT current_schema()"))
    )
    op.execute(f"ALTER TABLE long_job_cancel_audit DROP CONSTRAINT IF EXISTS {FK}")
    op.execute("ALTER TABLE watches ADD COLUMN IF NOT EXISTS last_fired_at timestamptz")
    op.execute(
        "UPDATE watches w SET last_fired_at = f.last FROM "
        "(SELECT watch_id, max(created_at) AS last FROM watch_firings GROUP BY watch_id) f "
        "WHERE f.watch_id = w.id"
    )
    op.execute(FUNCTION.format(schema=schema))
    op.execute("DROP TRIGGER IF EXISTS note_watch_firing ON watch_firings")
    op.execute(
        "CREATE TRIGGER note_watch_firing AFTER INSERT ON watch_firings "
        f"FOR EACH ROW EXECUTE FUNCTION {schema}.note_watch_firing()"
    )
    with op.get_context().autocommit_block():
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)"),
            {"n": INDEX},
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            "ON long_job_cancel_audit (requested_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
    op.execute("DROP TRIGGER IF EXISTS note_watch_firing ON watch_firings")
    op.execute("DROP FUNCTION IF EXISTS note_watch_firing()")
    op.execute("ALTER TABLE watches DROP COLUMN IF EXISTS last_fired_at")
    # Rows whose job is gone cannot satisfy the key; they were the job's own records.
    op.execute(
        "DELETE FROM long_job_cancel_audit a "
        "WHERE NOT EXISTS (SELECT 1 FROM long_job j WHERE j.id = a.id)"
    )
    op.execute(
        f"ALTER TABLE long_job_cancel_audit ADD CONSTRAINT {FK} "
        "FOREIGN KEY (id) REFERENCES long_job(id) ON DELETE CASCADE"
    )
