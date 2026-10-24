"""Encrypted tenant-scoped lead ledger, effect intents and human audit.

downgrade: unsupported
"""

from alembic import op

revision = "0118_lead_intake"
down_revision = "0117_clear_legacy_routine_output"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    statements = """
        CREATE TABLE lead_intake_configs (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            config JSONB NOT NULL, encrypted_credentials BYTEA NOT NULL);
        CREATE TABLE lead_intakes (
            tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL, source_digest TEXT NOT NULL, actor_digest TEXT, status TEXT NOT NULL,
            encrypted_payload BYTEA, encrypted_raw BYTEA,
            created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
            raw_expires_at TIMESTAMPTZ NOT NULL,
            destination_guild_id TEXT NOT NULL, destination_channel_id TEXT NOT NULL,
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_lead_source UNIQUE (tenant_id, source_digest));
        CREATE TABLE lead_intake_effects (
            tenant_id UUID NOT NULL, intake_id UUID NOT NULL, step TEXT NOT NULL,
            stable_key TEXT NOT NULL, state TEXT NOT NULL, remote_id TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (tenant_id, intake_id, step),
            FOREIGN KEY (tenant_id, intake_id) REFERENCES lead_intakes(tenant_id, id)
            ON DELETE CASCADE);
        CREATE UNIQUE INDEX uq_lead_discord_starter ON lead_intake_effects(tenant_id, remote_id)
            WHERE step = 'starter' AND remote_id IS NOT NULL;
        CREATE TABLE lead_resolution_audit (
            id UUID PRIMARY KEY, tenant_id UUID NOT NULL, intake_id UUID NOT NULL,
            encrypted_resolution BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL,
            FOREIGN KEY (tenant_id, intake_id) REFERENCES lead_intakes(tenant_id, id)
            ON DELETE CASCADE);
        CREATE TABLE lead_opportunities (
            tenant_id UUID NOT NULL, identity_digest TEXT NOT NULL, intake_id UUID NOT NULL,
            encrypted_mapping BYTEA NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (tenant_id, identity_digest),
            FOREIGN KEY (tenant_id, intake_id) REFERENCES lead_intakes(tenant_id, id)
            ON DELETE CASCADE);
        CREATE TABLE lead_mailbox_scans (
            tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE, id UUID NOT NULL,
            mailbox_key TEXT NOT NULL, window_start TIMESTAMPTZ NOT NULL,
            window_end TIMESTAMPTZ NOT NULL, state TEXT NOT NULL, discovered_count INTEGER NOT NULL,
            started_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ, error_code TEXT,
            PRIMARY KEY (tenant_id, id));
        CREATE TABLE lead_mailbox_messages (
            tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE, source_digest TEXT NOT NULL,
            mailbox_key TEXT NOT NULL, encrypted_message_id BYTEA, disposition TEXT NOT NULL,
            intake_id UUID, expires_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (tenant_id, source_digest),
            FOREIGN KEY (tenant_id, intake_id) REFERENCES lead_intakes(tenant_id, id)
            ON DELETE CASCADE);
        CREATE TABLE lead_intake_executions (
            tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL, routine_id UUID NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (tenant_id, id));
        CREATE TABLE lead_mailbox_states (
            tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
            mailbox_key TEXT NOT NULL, encrypted_state BYTEA NOT NULL,
            PRIMARY KEY (tenant_id, mailbox_key));
    """
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Lead evidence requires an explicit retention/export procedure")
