"""fleet_run_cost_owed: a fleet card shown with a cost still being counted owes an update.

A run's terminal card can be shown while a worker's usage isn't on the ledger yet
(an OpenAI turn first read with `usage: null`). The card then says "cost so far",
and the run's `end_notice` becomes 'cost_owed': the run-end poller edits the card
again with the settled cost. The owed-notice index covers both states. A downgrade
drops pending cost updates ('cost_owed' becomes 'shown'): such a card keeps "cost so far".

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0140_fleet_run_cost_owed"
down_revision: str | None = "0139_tenant_ledger_estimate"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint("ck_fleet_run_end_notice", "fleet_run", type_="check")
    op.create_check_constraint(
        "ck_fleet_run_end_notice",
        "fleet_run",
        "end_notice IS NULL OR end_notice IN ('owed', 'cost_owed', 'shown', 'given_up')",
    )
    op.drop_index("ix_fleet_run_end_notice_owed", table_name="fleet_run")
    op.create_index(
        "ix_fleet_run_end_notice_owed",
        "fleet_run",
        ["platform", "end_notice_due_at"],
        postgresql_where=sa.text("end_notice IN ('owed', 'cost_owed')"),
    )


def downgrade() -> None:
    op.execute("UPDATE fleet_run SET end_notice = 'shown' WHERE end_notice = 'cost_owed'")
    op.drop_index("ix_fleet_run_end_notice_owed", table_name="fleet_run")
    op.create_index(
        "ix_fleet_run_end_notice_owed",
        "fleet_run",
        ["platform", "end_notice_due_at"],
        postgresql_where=sa.text("end_notice = 'owed'"),
    )
    op.drop_constraint("ck_fleet_run_end_notice", "fleet_run", type_="check")
    op.create_check_constraint(
        "ck_fleet_run_end_notice",
        "fleet_run",
        "end_notice IS NULL OR end_notice IN ('owed', 'shown', 'given_up')",
    )
