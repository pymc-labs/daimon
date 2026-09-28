"""Lease columns that turn `task_continuations` into a durable wake queue.

A claim now carries an owner and an expiry, so a process that dies holding one
no longer strands it. `started_at` is the fence committed right before the
turn begins: an expired claim is retried only while it is NULL, and settled
instead once it is set. `available_at` makes a row pollable and not claimable
before that instant; NULL keeps today's rows dispatch-on-next-turn only.
`cancelled` lets a not-yet-claimed wake be withdrawn without deleting it.

Every column is nullable or defaulted, so existing rows are unchanged.
Downgrading settles every scheduled wake not yet run as `skipped/downgraded`,
so none runs early once `available_at` is gone.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028_feat003_wake_queue"
down_revision: str | None = "0028_sys074_routine_catch_up"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "task_continuations",
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("task_continuations", sa.Column("lease_owner", sa.Text(), nullable=True))
    op.add_column(
        "task_continuations",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_continuations",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_continuations",
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.drop_constraint("ck_task_continuations_status", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_status",
        "task_continuations",
        "status IN ('pending', 'claimed', 'delivered', 'skipped', 'cancelled')",
    )
    op.create_index(
        "task_continuations_due_idx",
        "task_continuations",
        ["platform", "status", "available_at"],
        postgresql_where=sa.text("available_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("task_continuations_due_idx", table_name="task_continuations")
    # Without `available_at` a scheduled wake would read as a handoff due at
    # the next turn, i.e. run early; settle every one not yet run instead.
    op.execute(
        "UPDATE task_continuations SET status = 'skipped', skip_reason = 'downgraded' "
        "WHERE available_at IS NOT NULL AND status IN ('pending', 'claimed')"
    )
    op.execute("UPDATE task_continuations SET status = 'skipped' WHERE status = 'cancelled'")
    op.drop_constraint("ck_task_continuations_status", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_status",
        "task_continuations",
        "status IN ('pending', 'claimed', 'delivered', 'skipped')",
    )
    op.drop_column("task_continuations", "attempts")
    op.drop_column("task_continuations", "started_at")
    op.drop_column("task_continuations", "lease_expires_at")
    op.drop_column("task_continuations", "lease_owner")
    op.drop_column("task_continuations", "available_at")
