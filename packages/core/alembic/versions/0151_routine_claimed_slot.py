"""Retain handover identity, claimed slots, seed T and completion evidence.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0151_routine_claimed_slot"
down_revision = "0150_origin_human_invocation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "routines", sa.Column("last_fired_slot", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "routines", sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "routines", sa.Column("last_finished_claim_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("routines", sa.Column("seeded_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("routines", sa.Column("internal_routine_id", postgresql.UUID(), nullable=True))
    # Exact first-line tag only: never infer identity from prose, another line,
    # a suffix or a similarly named tag. Duplicate imports fail the constraint
    # and roll back the migration rather than choosing one routine silently.
    op.execute(
        "UPDATE routines SET internal_routine_id = "
        "rtrim(split_part(split_part(trigger_message, chr(10), 1), ':', 2), chr(13))::uuid "
        "WHERE split_part(trigger_message, chr(10), 1) ~ "
        "'^internal-routine-import-v1:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
        "[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\r?$'"
    )
    op.create_unique_constraint(
        "uq_routines_tenant_internal_id", "routines", ["tenant_id", "internal_routine_id"]
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("uq_routines_tenant_internal_id", "routines", type_="unique")
    op.drop_column("routines", "internal_routine_id")
    op.drop_column("routines", "seeded_after")
    op.drop_column("routines", "last_finished_claim_at")
    op.drop_column("routines", "last_finished_at")
    op.drop_column("routines", "last_fired_slot")
