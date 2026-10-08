"""Let an approved GitHub request wake its unfinished chat once.

downgrade: destructive
"""

from alembic import op

revision: str = "0070_github_access_wakes"
down_revision: str | None = "0069_github_key_restart_notice"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_reason",
        "task_continuations",
        "reason IN ('task_handoff', 'private_input_applied', 'timer', 'github_access_ready')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM task_continuations WHERE reason = 'github_access_ready'")
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_reason",
        "task_continuations",
        "reason IN ('task_handoff', 'private_input_applied', 'timer')",
    )
