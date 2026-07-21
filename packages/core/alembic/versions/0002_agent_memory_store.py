"""agent_memory_store — per-(tenant, agent) MA memory store binding.

One row per (tenant, agent): maps daimon's derived agent UUID to the
Anthropic memory store id (memstore_...) attached to that agent's sessions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0002_agent_memory_store"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agent_memory_store",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("memory_store_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_agent_memory_store"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_agent_memory_store_tenant_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_memory_store")
