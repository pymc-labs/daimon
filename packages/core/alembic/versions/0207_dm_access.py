"""dm_access: per-agent direct message grants, routing rules and picks

Revision ID: 0207_dm_access
Revises: 0206_thread_model_effort

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0207_dm_access"
down_revision: str | None = "0206_thread_model_effort"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dm_agent_grants",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("who", sa.Text(), server_default="members", nullable=False),
        sa.Column("user_ids", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("group_ids", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "who IN ('members', 'listed', 'excluded')", name="ck_dm_agent_grants_who"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "agent_name"),
    )
    op.create_table(
        "dm_rules",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=True),
        sa.Column("agent_name", sa.Text(), nullable=True),
        sa.Column("locked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("subject IN ('user', 'group', 'everyone')", name="ck_dm_rules_subject"),
        sa.CheckConstraint(
            "(subject = 'everyone') = (subject_id IS NULL)", name="ck_dm_rules_subject_id"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "position"),
    )
    op.create_table(
        "dm_agent_picks",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "account_id"),
    )


def downgrade() -> None:
    op.drop_table("dm_agent_picks")
    op.drop_table("dm_rules")
    op.drop_table("dm_agent_grants")
