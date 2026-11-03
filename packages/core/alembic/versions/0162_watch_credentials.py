"""Owner-supplied watch credentials, and the private form that collects them.

`watch_credentials` holds one encrypted read key per (tenant, owner account,
canonical server URL). Only the app role reads it: the sidecar and the watch
ingress owner get nothing, and the sidecar privilege audit proves it. The
credential form gains the `watch_credential` kind.

downgrade: destructive
"""

from alembic import op

revision = "0162_watch_credentials"
down_revision: str | None = "0161_routine_source_id"
branch_labels = None
depends_on = None

KINDS = "'env', 'env_file', 'mcp', 'mcp_oauth', 'repo', 'skill_repo'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""CREATE TABLE watch_credentials (
        id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        owner_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        server_url TEXT NOT NULL,
        token_encrypted BYTEA NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        rotated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        last_used_at TIMESTAMP WITH TIME ZONE,
        CONSTRAINT uq_watch_credentials_owner_url
            UNIQUE (tenant_id, owner_account_id, server_url)
    )""")
    # The restricted watch identities must never reach a stored key.
    for role in ("daimon_sidecar", "daimon_watch_ingress_owner"):
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = '"
            + role
            + "') THEN EXECUTE format('REVOKE ALL ON watch_credentials FROM %I', '"
            + role
            + "'); END IF; END $$"
        )
    op.execute("REVOKE ALL ON watch_credentials FROM PUBLIC")
    op.execute("ALTER TABLE credential_requests DROP CONSTRAINT ck_credential_requests_kind")
    op.execute(
        "ALTER TABLE credential_requests ADD CONSTRAINT ck_credential_requests_kind "
        f"CHECK (kind IN ({KINDS}, 'watch_credential'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM credential_requests WHERE kind = 'watch_credential'")
    op.execute("ALTER TABLE credential_requests DROP CONSTRAINT ck_credential_requests_kind")
    op.execute(
        "ALTER TABLE credential_requests ADD CONSTRAINT ck_credential_requests_kind "
        f"CHECK (kind IN ({KINDS}))"
    )
    op.execute("DROP TABLE watch_credentials")
