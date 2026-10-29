"""Remove the staffing ledger.

Upgrade permanently removes all ledger data. Downgrade restores the empty
schema and its access restrictions, but cannot recover removed evidence.

downgrade: safe
"""

from alembic import op

revision = "0146_remove_recruitment"
down_revision = "0145_recruitment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Dropping the children first removes their foreign keys before the parent.
    # DROP TABLE also removes rows, indexes, constraints, policies and ACLs.
    op.execute("DROP TABLE recruitment_outbox")
    op.execute("DROP TABLE recruitment_messages")
    op.execute("DROP TABLE recruitment_threads")


def downgrade() -> None:
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
