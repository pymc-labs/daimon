"""Persist routine catch-up policy and the most recent skipped slot range.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_sys074_routine_catch_up"
down_revision = "0027_turn_card_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "routines", sa.Column("catch_up_policy", sa.Text(), nullable=False, server_default="skip")
    )
    op.add_column(
        "routines", sa.Column("last_skipped_from", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "routines", sa.Column("last_skipped_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("routines", sa.Column("last_skip_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_routines_catch_up_policy", "routines", "catch_up_policy IN ('skip', 'run-once')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_routines_catch_up_policy", "routines", type_="check")
    op.drop_column("routines", "last_skip_reason")
    op.drop_column("routines", "last_skipped_until")
    op.drop_column("routines", "last_skipped_from")
    op.drop_column("routines", "catch_up_policy")
