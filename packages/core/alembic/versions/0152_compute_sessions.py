"""Owner-isolated ephemeral compute sessions.

downgrade: unsupported
"""

from alembic import op

revision = "0152_compute_sessions"
down_revision = "0151_routine_claimed_slot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""CREATE TABLE compute_sessions (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        owner_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        owner_external_user_id TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        parent_channel_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        source_metadata JSONB DEFAULT '{}' NOT NULL,
        extension_keys JSONB DEFAULT '[]' NOT NULL,
        request_key TEXT NOT NULL,
        app_name TEXT NOT NULL,
        profile TEXT NOT NULL,
        image TEXT NOT NULL,
        region TEXT NOT NULL,
        cpu_kind TEXT NOT NULL,
        cpus INTEGER NOT NULL,
        memory_mb INTEGER NOT NULL,
        state TEXT DEFAULT 'launching' NOT NULL,
        machine_id TEXT,
        ttl_hours INTEGER NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        warned_for TIMESTAMP WITH TIME ZONE,
        CONSTRAINT uq_compute_request UNIQUE (tenant_id, request_key),
        CONSTRAINT uq_compute_app UNIQUE (app_name),
        CONSTRAINT ck_compute_state CHECK (state IN ('launching','running','stopping','deleted')),
        CONSTRAINT ck_compute_limits CHECK (ttl_hours BETWEEN 1 AND 168
            AND cpus > 0 AND memory_mb > 0)
    )""")
    op.execute(
        "CREATE INDEX ix_compute_owner ON compute_sessions (tenant_id, owner_account_id, state)"
    )
    op.execute("ALTER TABLE compute_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE compute_sessions FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_scope ON compute_sessions USING (tenant_id =
NULLIF(current_setting('daimon.tenant_id', true), '')::uuid) WITH CHECK (tenant_id =
NULLIF(current_setting('daimon.tenant_id', true), '')::uuid)""")


def downgrade() -> None:
    raise NotImplementedError(
        "Retain compute ownership records until provider cleanup is confirmed"
    )
