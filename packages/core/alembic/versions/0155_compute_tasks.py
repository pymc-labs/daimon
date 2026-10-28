"""Background commands on owner-isolated compute sessions.

downgrade: unsupported
"""

from alembic import op

revision = "0155_compute_tasks"
down_revision = "0154_repo_memory_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""CREATE TABLE compute_tasks (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id UUID NOT NULL REFERENCES compute_sessions(id) ON DELETE CASCADE,
        owner_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        request_key TEXT NOT NULL,
        command_preview TEXT NOT NULL,
        state TEXT DEFAULT 'starting' NOT NULL,
        exit_code INTEGER,
        timeout_minutes INTEGER NOT NULL,
        deadline_at TIMESTAMP WITH TIME ZONE NOT NULL,
        output_bytes BIGINT DEFAULT '0' NOT NULL,
        output_tail TEXT DEFAULT '' NOT NULL,
        output_path TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        finished_at TIMESTAMP WITH TIME ZONE,
        version INTEGER DEFAULT '0' NOT NULL,
        CONSTRAINT uq_compute_task_request UNIQUE (tenant_id, request_key),
        CONSTRAINT ck_compute_task_state CHECK (state IN
            ('starting','running','succeeded','failed','timed_out','lost','cancelled')),
        CONSTRAINT ck_compute_task_limits CHECK (timeout_minutes > 0 AND output_bytes >= 0)
    )""")
    op.execute(
        "CREATE INDEX ix_compute_task_session ON compute_tasks (tenant_id, session_id, state)"
    )
    op.execute("ALTER TABLE compute_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE compute_tasks FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_scope ON compute_tasks USING (tenant_id =
NULLIF(current_setting('daimon.tenant_id', true), '')::uuid) WITH CHECK (tenant_id =
NULLIF(current_setting('daimon.tenant_id', true), '')::uuid)""")


def downgrade() -> None:
    raise NotImplementedError("Retain compute task records until their sessions are deleted")
