"""Record a fleet worker's turn outcome under its own origin.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0095_turn_outcome_worker_origin"
down_revision: str | None = "0094_worker_finished_wakes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_turn_outcomes_origin", "turn_outcomes", type_="check")
    op.create_check_constraint(
        "ck_turn_outcomes_origin",
        "turn_outcomes",
        "origin IN ('chat', 'routine', 'relay', 'handoff', 'worker')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # A previous application has no worker origin; never relabel these
    # diagnostics as another origin's turns. They are content-free, so drop them.
    op.execute("DELETE FROM turn_outcomes WHERE origin = 'worker'")
    op.drop_constraint("ck_turn_outcomes_origin", "turn_outcomes", type_="check")
    op.create_check_constraint(
        "ck_turn_outcomes_origin",
        "turn_outcomes",
        "origin IN ('chat', 'routine', 'relay', 'handoff')",
    )
