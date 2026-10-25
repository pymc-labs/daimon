"""Remove Fleet storage and prevent new Fleet entries in shared history tables.

Applying this migration permanently deletes Fleet run/worker history and stored
Fleet grants. Back up that history before a separately approved rollout. Shared
turn/accounting/provider history is retained. NOT VALID checks reject new obsolete
reasons without deleting historical rows or requiring an immediate history rewrite.
This migration is irreversible; rollback requires a database backup.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0141_remove_fleet"
down_revision: str | None = "0140_fleet_run_cost_owed"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("fleet_worker")
    op.drop_table("fleet_run")
    op.drop_column("agent_backend_revision", "fleet_grant")
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.execute(
        "ALTER TABLE task_continuations ADD CONSTRAINT ck_task_continuations_reason "
        "CHECK (reason IN ('task_handoff', 'private_input_applied', 'timer', "
        "'github_access_ready')) NOT VALID"
    )
    op.drop_constraint("ck_turn_outcomes_origin", "turn_outcomes", type_="check")
    op.execute(
        "ALTER TABLE turn_outcomes ADD CONSTRAINT ck_turn_outcomes_origin "
        "CHECK (origin IN ('chat', 'routine', 'relay', 'handoff')) NOT VALID"
    )


def downgrade() -> None:
    raise RuntimeError("Fleet removal is irreversible; restore a database backup")
