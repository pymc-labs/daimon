"""mcp_connections: a workspace's shared MCP connections and their tools.

Two new tables, empty on upgrade and unread until the `shared_connections` feature is on, so
nothing changes for an existing workspace. A connection holds an upstream MCP server's address,
its bearer token encrypted, and an optional data scope; each tool holds the description and
schema an admin approved beside the ones the server last sent. Downgrading drops both tables
and with them every shared connection.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0260_mcp_connections"
down_revision: str | None = "0244_data_memory"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "mcp_connections",
        sa.Column(
            "id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("encrypted_auth", sa.LargeBinary(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("declared", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'error')", name="ck_mcp_connections_status"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_mcp_connections_tenant_name"),
    )
    op.create_table(
        "mcp_connection_tools",
        sa.Column("connection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("input_schema", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("seen_description", sa.Text(), server_default="", nullable=False),
        sa.Column("seen_schema", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("writes", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("approved_writes", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("writes_set", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["mcp_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("connection_id", "name"),
    )
    op.create_index("ix_mcp_connection_tools_tenant", "mcp_connection_tools", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("mcp_connection_tools")
    op.drop_table("mcp_connections")
