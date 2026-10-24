"""fleet_plans: a fleet run's plan, accepted before it runs.

With `fleet.require_plan` on, a chat turn's fleet spawn needs an accepted plan for its
thread (`daimon.core.fleet_plan`): the defaults come from the agent's fleet grant and
the models the person named, the person accepts or edits them, and every spawn stays
inside what was accepted. Off by default: nothing reads this table until it is on.

Numbered 0138 (0136 and 0137 are reserved by other branches); it sits on the current
head and is re-parented at rebase, so there is always one head.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0138_fleet_plans"
down_revision: str | None = "0135_mcp_token_skill_bytes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "fleet_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'cancelled')", name="ck_fleet_plans_status"
        ),
    )
    op.create_index(
        "ix_fleet_plans_thread",
        "fleet_plans",
        ["tenant_id", "platform", "channel_id", "thread_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_fleet_plans_thread", table_name="fleet_plans")
    op.drop_table("fleet_plans")
