"""Remember where to deliver a self-serve GitHub connect follow-up.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0074_github_connect_followup"
down_revision: str | None = "0073_github_removal_notice"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    for name in (
        "requester_platform_user_id",
        "origin_platform",
        "origin_parent_channel_id",
        "origin_thread_id",
        "origin_ma_agent_id",
        "origin_responder_name",
        "requested_work",
    ):
        op.add_column("github_connect_invitations", sa.Column(name, sa.Text()))
    op.add_column("github_connect_invitations", sa.Column("connected_repos", JSONB()))
    op.add_column(
        "github_connect_invitations", sa.Column("encrypted_origin_followup", sa.LargeBinary())
    )
    op.add_column(
        "github_connect_invitations",
        sa.Column("origin_followup_expires_at", sa.DateTime(timezone=True)),
    )
    for name in ("notice_claimed_at", "notice_delivered_at"):
        op.add_column("github_connect_invitations", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column(
        "github_connect_invitations",
        sa.Column("notice_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "github_connect_invitations",
        sa.Column("notice_next_attempt_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "github_connect_click_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "requester_account_id",
            sa.Uuid(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requester_platform_user_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("origin_parent_channel_id", sa.Text(), nullable=False),
        sa.Column("origin_thread_id", sa.Text(), nullable=False),
        sa.Column("origin_ma_agent_id", sa.Text(), nullable=False),
        sa.Column("origin_responder_name", sa.Text(), nullable=False),
        sa.Column("requested_work", sa.Text()),
        sa.Column("encrypted_token", sa.LargeBinary()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("github_connect_click_intents")
    for name in (
        "notice_next_attempt_at",
        "notice_attempts",
        "notice_delivered_at",
        "notice_claimed_at",
        "origin_followup_expires_at",
        "encrypted_origin_followup",
        "connected_repos",
        "requested_work",
        "origin_ma_agent_id",
        "origin_responder_name",
        "origin_thread_id",
        "origin_parent_channel_id",
        "origin_platform",
        "requester_platform_user_id",
    ):
        op.drop_column("github_connect_invitations", name)
