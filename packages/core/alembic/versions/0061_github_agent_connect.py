"""Bind GitHub connection invitations and requests to one agent.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0061_github_agent_connect"
down_revision: str | None = "0060_platform_names"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("github_connect_invitations", sa.Column("agent_id", sa.UUID(), nullable=True))
    op.add_column("github_connect_invitations", sa.Column("agent_name", sa.Text(), nullable=True))
    op.add_column(
        "github_connect_invitations", sa.Column("activation_status", sa.Text(), nullable=True)
    )
    op.add_column(
        "github_connect_invitations", sa.Column("connected_repo_count", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "ck_github_connect_invitation_activation",
        "github_connect_invitations",
        "activation_status IS NULL OR activation_status IN ('activated', 'update_pending')",
    )
    op.create_table(
        "github_connect_requests",
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
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "tenant_id", "requester_account_id", "agent_id", name="uq_github_connect_request"
        ),
    )


def downgrade() -> None:
    op.drop_table("github_connect_requests")
    op.drop_constraint("ck_github_connect_invitation_activation", "github_connect_invitations")
    op.drop_column("github_connect_invitations", "activation_status")
    op.drop_column("github_connect_invitations", "connected_repo_count")
    op.drop_column("github_connect_invitations", "agent_name")
    op.drop_column("github_connect_invitations", "agent_id")
