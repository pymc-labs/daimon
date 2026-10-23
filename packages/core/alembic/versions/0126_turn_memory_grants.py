"""turn_memory_grants: which memory one provider turn's MCP credential may use.

A row per `provider_turn` credential (jti), written with the token from the
turn's admission: the agent, the caller's account, whether the caller's
personal store applies, the read-only posture, and the place. The MCP memory
tools read it only by the credential's jti. Rows go with their token
(ON DELETE CASCADE) and are short-lived like it. Additive; downgrading drops the
table, and the memory tools then refuse every call (no grant).

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0126_turn_memory_grants"
# The number is reserved (LEAD); down_revision = main's head at the last pre-merge rebase.
down_revision: str | None = "0123_tenant_backends_explicit"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "turn_memory_grants",
        sa.Column(
            "jti",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mcp_tokens.jti", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("personal", sa.Boolean(), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("is_dm", sa.Boolean(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("turn_memory_grants")
