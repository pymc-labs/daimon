"""agent_co_owners: an agent may have several owners.

`agent_owner` keeps the one primary owner and its meaning. `agent_co_owners`
adds further accounts that own the agent exactly as the primary owner does:
`(tenant_id, agent_id, account_id)` is the key, `added_by_account_id` names the
admin who added the person (null when that admin's account is gone), and
`added_at` records when. Deleting a co-owner's account deletes the row; the
agent keeps its other owners.

Nothing is backfilled. Additive only; downgrading drops the table and every
agent falls back to its primary owner alone.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0143_agent_co_owners"
down_revision: str | None = "0142_mux_account_erasure"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "agent_co_owners",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("added_by_account_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "account_id", name="pk_agent_co_owners"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_agent_co_owners_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE", name="fk_agent_co_owners_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["added_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_agent_co_owners_added_by",
        ),
    )
    op.create_index("ix_agent_co_owners_account", "agent_co_owners", ["account_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("agent_co_owners")
