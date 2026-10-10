"""Bind conversational GitHub grants to a later requester turn.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0080_github_grant_proposals"
down_revision: str | None = "0079_github_agent_repos"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "github_grant_proposals",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "requester_account_id",
            sa.UUID(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requester_platform_user_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("repo_name", sa.Text(), nullable=False),
        sa.Column("ability", sa.Text(), nullable=False),
        sa.Column("origin_id", sa.UUID(), nullable=False),
        sa.Column("approved_origin_id", sa.UUID()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "requester_account_id",
            "platform",
            "requester_platform_user_id",
            "thread_id",
            name="uq_github_grant_proposal",
        ),
    )
    op.create_index("github_grant_proposals_expiry_idx", "github_grant_proposals", ["expires_at"])


def downgrade() -> None:
    op.drop_table("github_grant_proposals")
