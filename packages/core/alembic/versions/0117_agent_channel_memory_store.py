"""agent_channel_memory_store: the memory a held channel shares.

One row per (tenant, agent, platform, channel): the Anthropic memory store
(memstore_...) a held turn mounts instead of the agent-wide store, and with no
personal store. A held turn is one from another organisation or in a channel
shared with another organisation. `channel_id` is the parent channel; threads
under it use the same store. Created lazily on the first held session there;
nothing is backfilled and the agent-wide and personal stores are untouched.
Additive only; downgrading drops the bindings and the MA stores remain.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0117_agent_channel_memory_store"
down_revision: str | None = "0116_shared_channel_facts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "agent_channel_memory_store",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("memory_store_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "agent_id", "platform", "channel_id", name="pk_agent_channel_memory_store"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_agent_channel_memory_store_tenants",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_channel_memory_store")
