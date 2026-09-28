"""Append-only tenant security audit metadata.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029_sys050_security_audit"
down_revision = "0030_sys066_turn_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "security_audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True)),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True)),
        sa.Column("platform", sa.Text()),
        sa.Column("platform_user_id", sa.Text()),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text()),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "outcome IN ('allowed', 'denied', 'error')", name="ck_security_audit_outcome"
        ),
    )
    op.create_index(
        "ix_security_audit_tenant_time", "security_audit_events", ["tenant_id", "occurred_at", "id"]
    )
    op.execute("""
        CREATE FUNCTION reject_security_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP <> 'TRUNCATE' AND
               current_setting('daimon.security_audit_maintenance', true) = 'on' THEN
                RETURN NULL;
            END IF;
            RAISE EXCEPTION 'security audit events are append-only';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER security_audit_no_mutation
        BEFORE UPDATE OR DELETE OR TRUNCATE ON security_audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_security_audit_mutation()
    """)

    # Old privacy workers know nothing about audit rows. Enforce erasure at the
    # database boundary as well as in current store helpers during rolling upgrades.
    # Qualify the target with the triggering table's schema, not caller search_path.
    op.execute("""
        CREATE FUNCTION erase_security_audit_account() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE previous_mode text;
        BEGIN
            previous_mode := current_setting('daimon.security_audit_maintenance', true);
            PERFORM set_config('daimon.security_audit_maintenance', 'on', true);
            EXECUTE format(
                'UPDATE %I.security_audit_events SET account_id = NULL, '
                'platform_user_id = NULL WHERE account_id = $1', TG_TABLE_SCHEMA
            ) USING OLD.id;
            PERFORM set_config(
                'daimon.security_audit_maintenance', coalesce(previous_mode, 'off'), true
            );
            RETURN OLD;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER security_audit_account_erasure
        AFTER DELETE ON accounts
        FOR EACH ROW EXECUTE FUNCTION erase_security_audit_account()
    """)
    op.execute("""
        CREATE FUNCTION erase_security_audit_tenant() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE previous_mode text;
        BEGIN
            previous_mode := current_setting('daimon.security_audit_maintenance', true);
            PERFORM set_config('daimon.security_audit_maintenance', 'on', true);
            EXECUTE format(
                'DELETE FROM %I.security_audit_events WHERE tenant_id = $1', TG_TABLE_SCHEMA
            ) USING OLD.id;
            PERFORM set_config(
                'daimon.security_audit_maintenance', coalesce(previous_mode, 'off'), true
            );
            RETURN OLD;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER security_audit_tenant_erasure
        AFTER DELETE ON tenants
        FOR EACH ROW EXECUTE FUNCTION erase_security_audit_tenant()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER security_audit_account_erasure ON accounts")
    op.execute("DROP TRIGGER security_audit_tenant_erasure ON tenants")
    op.execute("DROP FUNCTION erase_security_audit_account()")
    op.execute("DROP FUNCTION erase_security_audit_tenant()")
    op.drop_table("security_audit_events")
    op.execute("DROP FUNCTION reject_security_audit_mutation()")
