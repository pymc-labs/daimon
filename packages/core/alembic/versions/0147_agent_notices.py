"""agent_notices: failures the agent hears of on the next turn in a thread.

One row per notice for `(tenant_id, platform, thread_id, agent_id)`; the same
text is kept once. That agent's next turn in the thread takes and deletes them. Holds no
person's data. Additive only; downgrading drops the pending notices.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0147_agent_notices"
down_revision: str | None = "0146_isolated_fresh_start"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "agent_notices",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_notices"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_agent_notices_tenants"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "platform",
            "thread_id",
            "agent_id",
            "text",
            name="uq_agent_notices_thread_agent_text",
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("agent_notices")
