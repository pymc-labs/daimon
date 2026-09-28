"""Optional routine destination and the delivery outbox for its result tail.

A routine may name a channel or thread. After a successful fire whose agent did
not post there itself, the scheduler marks the row `delivery_status='pending'`
and the chat adapter for the tenant's platform posts `last_result_tail`
(claim → post → settle, at most once). Every column is nullable, so existing
routines have no destination and behave exactly as before.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_feat085_routine_destination"
down_revision = "0029_sys081_turn_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("routines", sa.Column("destination_kind", sa.Text(), nullable=True))
    op.add_column("routines", sa.Column("destination_id", sa.Text(), nullable=True))
    op.add_column("routines", sa.Column("delivery_status", sa.Text(), nullable=True))
    op.add_column("routines", sa.Column("delivery_lease_owner", sa.Text(), nullable=True))
    op.add_column(
        "routines",
        sa.Column("delivery_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("routines", sa.Column("delivery_note", sa.Text(), nullable=True))
    op.add_column("routines", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_routines_destination_kind",
        "routines",
        "destination_kind IS NULL OR destination_kind IN ('channel', 'thread')",
    )
    op.create_check_constraint(
        "ck_routines_destination_pair",
        "routines",
        "(destination_kind IS NULL) = (destination_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_routines_delivery_status",
        "routines",
        "delivery_status IS NULL OR delivery_status IN "
        "('pending', 'claimed', 'delivered', 'skipped')",
    )
    op.create_index(
        "routines_delivery_due_idx",
        "routines",
        ["delivery_status"],
        postgresql_where=sa.text("delivery_status IN ('pending', 'claimed')"),
    )


def downgrade() -> None:
    op.drop_index("routines_delivery_due_idx", table_name="routines")
    op.drop_constraint("ck_routines_delivery_status", "routines", type_="check")
    op.drop_constraint("ck_routines_destination_pair", "routines", type_="check")
    op.drop_constraint("ck_routines_destination_kind", "routines", type_="check")
    op.drop_column("routines", "delivered_at")
    op.drop_column("routines", "delivery_note")
    op.drop_column("routines", "delivery_lease_expires_at")
    op.drop_column("routines", "delivery_lease_owner")
    op.drop_column("routines", "delivery_status")
    op.drop_column("routines", "destination_id")
    op.drop_column("routines", "destination_kind")
