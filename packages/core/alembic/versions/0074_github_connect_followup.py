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
    ):
        op.add_column("github_connect_invitations", sa.Column(name, sa.Text()))
    op.add_column("github_connect_invitations", sa.Column("connected_repos", JSONB()))
    for name in ("notice_claimed_at", "notice_delivered_at"):
        op.add_column("github_connect_invitations", sa.Column(name, sa.DateTime(timezone=True)))


def downgrade() -> None:
    for name in (
        "notice_delivered_at",
        "notice_claimed_at",
        "connected_repos",
        "origin_thread_id",
        "origin_parent_channel_id",
        "origin_platform",
        "requester_platform_user_id",
    ):
        op.drop_column("github_connect_invitations", name)
