"""fleet_run_end_notice: whether a fleet run's thread was told the run ended.

A run that ends is owed a terminal card and, when no wake delivered its result,
a plain "The fleet ended" line. Any terminal card an adapter renders settles it;
otherwise the adapter's poller does (`daimon.core.fleet_run_end`). The card's
message id is kept so a retry edits that card instead of posting another.

Additive and nullable: existing runs owe nothing. Downgrading drops the columns;
an owed notice is then never posted (the card stays as it was, as before).

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0128_fleet_run_end_notice"
down_revision: str | None = "0123_tenant_backends_explicit"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("fleet_run", sa.Column("end_notice", sa.Text(), nullable=True))
    op.add_column(
        "fleet_run",
        sa.Column("end_notice_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "fleet_run", sa.Column("end_notice_due_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("fleet_run", sa.Column("card_message_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_fleet_run_end_notice",
        "fleet_run",
        "end_notice IS NULL OR end_notice IN ('owed', 'shown', 'given_up')",
    )
    op.create_index(
        "ix_fleet_run_end_notice_owed",
        "fleet_run",
        ["platform", "end_notice_due_at"],
        postgresql_where=sa.text("end_notice = 'owed'"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_fleet_run_end_notice_owed", table_name="fleet_run")
    op.drop_constraint("ck_fleet_run_end_notice", "fleet_run", type_="check")
    op.drop_column("fleet_run", "card_message_id")
    op.drop_column("fleet_run", "end_notice_due_at")
    op.drop_column("fleet_run", "end_notice_attempts")
    op.drop_column("fleet_run", "end_notice")
