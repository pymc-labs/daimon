"""record conversation activity for retention; cascade slack turn contexts with the tenant

`thread_sessions.last_turn_at` and `journal_session.last_appended_at` say when a
conversation was last used, so the retention sweep can age it. Both start at the
migration's time; existing mappings then take their newest known turn, in short batches
outside the migration transaction, so no hot table stays locked for the backfill, and a
value a new turn has moved on is left alone. Existing journal heads keep the migration's
time, since nothing recorded their last append. `slack_turn_contexts` gains its missing
tenant foreign key, after rows of tenants already deleted are dropped.

Revision ID: 0234_retention_activity
Revises: 0233_turn_origin_public_thread

downgrade: safe
schema: contract
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0234_retention_activity"
down_revision: str | None = "0233_turn_origin_public_thread"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK = "fk_slack_turn_contexts_tenant"
BACKFILL_BATCH = 1000


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    # Existing rows take this run's now(); a turn since then stamps a later time.
    stamp = bind.scalar(sa.text("SELECT now()"))
    # Idempotent up to the backfill, which commits as it goes: a failed run is rerun whole.
    op.execute(
        "DELETE FROM slack_turn_contexts c "
        "WHERE NOT EXISTS (SELECT 1 FROM tenants t WHERE t.id = c.tenant_id)"
    )
    op.execute(f"ALTER TABLE slack_turn_contexts DROP CONSTRAINT IF EXISTS {FK}")
    op.execute(
        f"ALTER TABLE slack_turn_contexts ADD CONSTRAINT {FK} FOREIGN KEY (tenant_id) "
        "REFERENCES tenants (id) ON DELETE CASCADE NOT VALID"
    )
    # Last, so their exclusive locks cover only instant statements until the commit.
    op.execute(
        "ALTER TABLE thread_sessions "
        "ADD COLUMN IF NOT EXISTS last_turn_at timestamptz NOT NULL DEFAULT now()"
    )
    op.execute(
        "ALTER TABLE journal_session "
        "ADD COLUMN IF NOT EXISTS last_appended_at timestamptz NOT NULL DEFAULT now()"
    )
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE slack_turn_contexts VALIDATE CONSTRAINT {FK}")
        # One read of turn_outcomes, which has no index on session_id; it locks nothing.
        op.execute("DROP TABLE IF EXISTS last_turns")
        op.execute(
            "CREATE TEMP TABLE last_turns AS SELECT session_id, max(started_at) AS started_at "
            "FROM turn_outcomes WHERE session_id IS NOT NULL GROUP BY session_id"
        )
        op.execute("CREATE UNIQUE INDEX ON last_turns (session_id)")
        op.execute("ANALYZE last_turns")
        after = uuid.UUID(int=0)
        while True:
            ids = list(
                bind.scalars(
                    sa.text(
                        "SELECT id FROM thread_sessions WHERE id > :after ORDER BY id LIMIT :n"
                    ),
                    {"after": after, "n": BACKFILL_BATCH},
                )
            )
            if not ids:
                break
            # Each batch commits on its own; a row a turn has stamped since is skipped.
            bind.execute(
                sa.text(
                    "UPDATE thread_sessions t SET last_turn_at = GREATEST(t.created_at, "
                    "t.updated_at, t.active_turn_started_at, (SELECT o.started_at FROM "
                    "last_turns o WHERE o.session_id = t.ma_session_id)) "
                    "WHERE t.id = ANY(:ids) AND t.last_turn_at <= :stamp"
                ).bindparams(sa.bindparam("ids", type_=sa.ARRAY(sa.Uuid()))),
                {"ids": ids, "stamp": stamp},
            )
            after = ids[-1]
        op.execute("DROP TABLE last_turns")


def downgrade() -> None:
    op.drop_constraint(FK, "slack_turn_contexts", type_="foreignkey")
    op.drop_column("journal_session", "last_appended_at")
    op.drop_column("thread_sessions", "last_turn_at")
