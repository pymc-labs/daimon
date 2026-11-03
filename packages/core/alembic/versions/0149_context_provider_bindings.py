"""Tenant-scoped pre-turn provider bindings.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0149_context_provider_bindings"
down_revision = "0148_daimon_admins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "context_provider_bindings",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("agent_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "channel_id", "agent_id"),
    )
    op.execute("ALTER TABLE context_provider_bindings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE context_provider_bindings FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY context_provider_tenant ON context_provider_bindings
        USING (tenant_id = nullif(current_setting('daimon.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = nullif(current_setting('daimon.tenant_id', true), '')::uuid)
    """)


def downgrade() -> None:
    op.drop_table("context_provider_bindings")
