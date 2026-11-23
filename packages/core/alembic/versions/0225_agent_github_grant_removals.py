"""agent_github_grant_removals: the repos an admin took off an agent.

An agent that follows its GitHub org holds every repo of the installation except the ones
taken off it. Removing a grant now leaves a row here, and granting the repo again clears it.
Agents that already had repos removed have no row, so those repos can come back once; the
table is empty on upgrade. Downgrading drops the table.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0225_agent_github_grant_removals"
down_revision: str | None = "0224_ops_events"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "agent_github_grant_removals",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "removed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "repo_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("agent_github_grant_removals")
