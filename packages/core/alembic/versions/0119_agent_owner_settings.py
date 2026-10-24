"""agent_owner_settings: an agent's own thread mode, profile and requirements, and its owner.

`agent_backend_revision` gains `profile`, `requires` and `thread_mode`. Null
is the backend's default, which is every revision written before these
columns existed, so no digest changes.

`agent_owner` records at most one owner per agent: the account that may edit
it without being a server admin. Nothing is backfilled. No stored creation
record names the account behind an existing agent: a chat-created agent is
stamped with the workspace account, and the audit row of its `create_agent`
call names the calling agent, not the created one. Every existing agent
therefore stays admin-only until an admin assigns an owner. Deleting the
account deletes the row, and the agent is admin-only again.

Additive only; downgrading drops the columns and the table. Every agent falls
back to its backend's defaults and to admin-only edits.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0119_agent_owner_settings"
down_revision: str | None = "0118_agent_backend_model"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("agent_backend_revision", sa.Column("profile", sa.Text(), nullable=True))
    op.add_column("agent_backend_revision", sa.Column("requires", JSONB(), nullable=True))
    op.add_column("agent_backend_revision", sa.Column("thread_mode", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_agent_backend_thread_mode",
        "agent_backend_revision",
        "thread_mode IS NULL OR thread_mode IN ('per_caller', 'shared')",
    )
    op.create_table(
        "agent_owner",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("owner_account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_agent_owner"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_agent_owner_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["owner_account_id"],
            ["accounts.id"],
            ondelete="CASCADE",
            name="fk_agent_owner_accounts",
        ),
        sa.CheckConstraint("source IN ('created', 'admin_assigned')", name="ck_agent_owner_source"),
    )
    op.create_index("ix_agent_owner_account", "agent_owner", ["owner_account_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("agent_owner")
    op.drop_constraint("ck_agent_backend_thread_mode", "agent_backend_revision", type_="check")
    op.drop_column("agent_backend_revision", "thread_mode")
    op.drop_column("agent_backend_revision", "requires")
    op.drop_column("agent_backend_revision", "profile")
