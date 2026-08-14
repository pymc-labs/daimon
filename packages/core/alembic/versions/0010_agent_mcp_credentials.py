"""Agent-scoped storage for an external MCP server's bearer token.

Revision ID: 0010_agent_mcp_credentials
Revises: 0009_seeded_skills
Create Date: 2026-08-14

downgrade: safe

No backfill is possible. MA vault credentials are write-only, so the tokens
already sitting in individual callers' vaults cannot be read back out — each
agent-level external MCP server needs its token re-entered once for the
per-session mirror to have anything to mirror. Until then those servers keep
working for whoever attached them and keep failing for everyone else, which is
exactly the pre-migration behaviour.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_agent_mcp_credentials"
down_revision: str | None = "0009_seeded_skills"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agent_mcp_credentials",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("mcp_server_url", sa.Text(), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_agent_mcp_credentials"),
        sa.UniqueConstraint(
            "tenant_id",
            "agent_id",
            "mcp_server_url",
            name="uq_agent_mcp_credentials_tenant_agent_url",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_mcp_credentials")
