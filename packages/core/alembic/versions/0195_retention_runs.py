"""remember when each retention rule last ran, and index the columns it filters on

`retention_runs` keeps a rule's last attempt and success, so a restart does not rerun a
sweep that finished that day. The indexes cover the cutoff each rule ages records by,
which the batched deletes scan in order. They are built concurrently, outside the
migration transaction, so writes continue; a run cut off mid-build leaves the table and
an invalid index, which a retried run keeps and drops and builds again.

Revision ID: 0195_retention_runs
Revises: 0194_tenant_retirement

downgrade: safe
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0195_retention_runs"
down_revision: str | None = "0194_tenant_retirement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLOSED = "'delivered', 'skipped', 'cancelled'"
INDEXES: dict[str, str] = {
    "ix_thread_sessions_tenant_last_turn": "thread_sessions (tenant_id, last_turn_at)",
    "ix_credential_requests_closed_at": "credential_requests (coalesce(used_at, expires_at))",
    "ix_agent_posted_messages_tenant_posted": "agent_posted_messages (tenant_id, posted_at)",
    "ix_message_feedback_created_at": "message_feedback (created_at)",
    "ix_support_escalations_created_at": "support_escalations (created_at)",
    "ix_task_continuations_closed_at": (
        "task_continuations (coalesce(delivered_at, claimed_at, created_at)) "
        f"WHERE status IN ({_CLOSED})"
    ),
    "ix_long_job_finished_at": "long_job (finished_at)",
    "ix_compute_tasks_finished_at": "compute_tasks (finished_at)",
    "ix_routines_last_finished_at": "routines (last_finished_at)",
    "ix_github_access_requests_updated_at": "github_access_requests (updated_at)",
    "ix_platform_channel_names_seen": "platform_channel_names (coalesce(last_seen_at, updated_at))",
    "ix_channel_changes_at": "channel_changes (at)",
    "ix_web_audit_at": "web_audit (at)",
    "ix_web_logins_created_at": "web_logins (created_at)",
    "ix_web_threads_deleted_at": "web_threads (deleted_at) WHERE deleted_at IS NOT NULL",
    "ix_web_threads_tenant_updated": "web_threads (tenant_id, updated_at) WHERE deleted_at IS NULL",
}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # IF NOT EXISTS: the index block below commits this, so a retried run finds the table.
    op.execute(
        "CREATE TABLE IF NOT EXISTS retention_runs ("
        "rule text PRIMARY KEY, "
        "last_attempt_at timestamptz NOT NULL, "
        "last_success_at timestamptz)"
    )
    with op.get_context().autocommit_block():
        for name, definition in INDEXES.items():
            invalid = op.get_bind().scalar(
                sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)"),
                {"n": name},
            )
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            table, _, rest = definition.partition(" ")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {rest}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS retention_runs")
