"""Settle the wake reasons after joining main and preview.

0141_remove_fleet (main) and 0163_watch_delivery (preview) each replace
ck_task_continuations_reason, so after the merge the list depends on which
branch ran last. This sets the union without the Fleet reason
'worker_finished'. NOT VALID keeps historical worker_finished rows, as
0141 does.

downgrade: safe
"""

from __future__ import annotations

from alembic import op

revision: str = "0169_continuation_reasons"
down_revision: str | None = "0168_merge_main_preview"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.execute(
        "ALTER TABLE task_continuations ADD CONSTRAINT ck_task_continuations_reason "
        "CHECK (reason IN ('task_handoff', 'private_input_applied', 'timer', "
        "'github_access_ready', 'watch_fired', 'job_finished', 'watch_report')) NOT VALID"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.execute(
        "ALTER TABLE task_continuations ADD CONSTRAINT ck_task_continuations_reason "
        "CHECK (reason IN ('task_handoff', 'private_input_applied', 'timer', "
        "'github_access_ready', 'worker_finished', 'watch_fired', 'job_finished', "
        "'watch_report')) NOT VALID"
    )
