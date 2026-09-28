"""Allow `timer` as a `task_continuations` reason: one-shot timers on the wake queue.

A timer is a wake row with `reason = 'timer'` and `available_at` set to its
fire time; nothing else about the table changes.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0029_feat084_timers"
down_revision: str | None = "0028_feat003_wake_queue"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_reason",
        "task_continuations",
        "reason IN ('task_handoff', 'private_input_applied', 'timer')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM task_continuations WHERE reason = 'timer'")
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_reason",
        "task_continuations",
        "reason IN ('task_handoff', 'private_input_applied')",
    )
