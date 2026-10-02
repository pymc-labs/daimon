"""Extra skills a channel's turns run with, on top of the agent's own.

`channel_skills` lists, per channel, the skills a session started there adds
to whatever agent answers. Each row pins the version picked, so creating a
session and checking it for drift build the same skill list. No row means
nothing extra, so existing tenants behave exactly as before.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042_channel_skills"
down_revision: str | None = "0041_channel_budget_notices"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "channel_skills",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner_agent_name", sa.Text(), nullable=True),
        sa.Column("added_by_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "platform", "channel_id", "skill_id", name="pk_channel_skills"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_channel_skills_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["added_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_channel_skills_added_by_account_id",
        ),
        sa.CheckConstraint(
            "platform IN ('discord', 'slack', 'teams')", name="ck_channel_skills_platform"
        ),
    )


def downgrade() -> None:
    op.drop_table("channel_skills")
