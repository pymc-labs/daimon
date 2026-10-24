"""Encrypted tenant-bound staffing ledger and durable backend intents.

downgrade: unsupported
"""

from alembic import op

revision = "0122_recruitment"
down_revision = "0121_routine_sandbox_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """CREATE TABLE recruitment_threads (
    tenant_id UUID NOT NULL,
    thread_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    seen_watermark BIGINT NOT NULL DEFAULT 0,
    covered_watermark BIGINT NOT NULL DEFAULT 0,
    card_id TEXT,
    claim_token TEXT,
    phase TEXT NOT NULL,
    revision TEXT NOT NULL,
    encrypted_state BYTEA,
    raw_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (tenant_id, thread_id),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
)"""
    )
    op.execute(
        """CREATE TABLE recruitment_messages (
    tenant_id UUID NOT NULL,
    thread_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    raw_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source_channel_id TEXT NOT NULL,
    needs_enrichment BOOLEAN NOT NULL,
    deleted BOOLEAN NOT NULL,
    processed_revision TEXT,
    revision TEXT NOT NULL,
    encrypted_source BYTEA,
    PRIMARY KEY (tenant_id, thread_id, message_id),
    FOREIGN KEY(tenant_id, thread_id) REFERENCES recruitment_threads (tenant_id, thread_id)
        ON DELETE CASCADE
)"""
    )
    op.execute(
        """CREATE TABLE recruitment_outbox (
    tenant_id UUID NOT NULL,
    id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    stable_key TEXT NOT NULL,
    state TEXT NOT NULL,
    encrypted_request BYTEA,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY(tenant_id, thread_id) REFERENCES recruitment_threads (tenant_id, thread_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_recruitment_key UNIQUE (tenant_id, stable_key),
    CONSTRAINT uq_recruitment_revision UNIQUE (tenant_id, thread_id, revision)
)"""
    )
    # Restricted detector identities must never acquire access to candidate data.
    for role in ("daimon_sidecar", "daimon_watch_ingress_owner"):
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = '"
            + role
            + "') THEN EXECUTE format('REVOKE ALL ON "
            "recruitment_threads, recruitment_messages, recruitment_outbox FROM %I', '"
            + role
            + "'); END IF; END $$"
        )
    op.execute(
        "REVOKE ALL ON recruitment_threads, recruitment_messages, recruitment_outbox FROM PUBLIC"
    )


def downgrade() -> None:
    raise NotImplementedError("Recruitment evidence requires an explicit retention procedure")
