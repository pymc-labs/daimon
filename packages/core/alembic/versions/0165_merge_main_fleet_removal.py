"""Join main's revisions since 0131 with the preview revisions since 0131.

Main added 0132_thread_handoff_summary -> ... -> 0146_isolated_fresh_start,
including 0141_remove_fleet. Preview added 0134_cold_dm_agent -> ... ->
0164_watch_lifetime_caps. Neither branch is renumbered: either may already be
applied. Only the task continuation reason check is touched by both, and each
branch rebuilds it without the other's reasons, so the result depends on which
branch ran last. This revision sets the one the models declare: the watch and
job reasons stay, and the removed worker completion reason is refused. NOT
VALID, as in 0141_remove_fleet, so historical worker completion rows remain.

downgrade: safe
"""

from __future__ import annotations

from alembic import op

revision: str = "0165_merge_main_fleet_removal"
down_revision: tuple[str, str] = ("0164_watch_lifetime_caps", "0146_isolated_fresh_start")
branch_labels: str | None = None
depends_on: str | None = None

_REASONS = (
    "'task_handoff', 'private_input_applied', 'timer', 'github_access_ready', "
    "'watch_fired', 'job_finished', 'watch_report'"
)


def _reason_check(reasons: str) -> None:
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.execute(
        "ALTER TABLE task_continuations ADD CONSTRAINT ck_task_continuations_reason "
        f"CHECK (reason IN ({reasons})) NOT VALID"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _reason_check(_REASONS)


def downgrade() -> None:
    # Both parents accept this superset, whichever branch is reversed first.
    op.execute("SET LOCAL lock_timeout = '5s'")
    _reason_check(f"{_REASONS}, 'worker_finished'")
