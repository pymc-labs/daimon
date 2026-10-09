"""Keep GitHub access requests until a decision or seven-day expiry.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0067_github_access_requests"
down_revision: str | None = "0066_github_panel_notices"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "github_access_requests",
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
        sa.Column("parent_channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("ma_agent_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("repo_names", JSONB(), nullable=False),
        sa.Column("requested_work", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admin_notified_at", sa.DateTime(timezone=True)),
        sa.Column("resumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('open', 'waiting_github', 'ready', 'cancelled', 'declined', 'expired')"
        ),
    )
    op.create_index(
        "ix_github_access_requests_waiting",
        "github_access_requests",
        ["tenant_id", "status", "expires_at"],
    )
    op.create_index(
        "uq_github_access_request_open_thread_asker_agent",
        "github_access_requests",
        ["tenant_id", "platform", "thread_id", "requester_account_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'waiting_github')"),
    )
    op.create_table(
        "github_access_request_deliveries",
        sa.Column(
            "request_id",
            sa.UUID(),
            sa.ForeignKey("github_access_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recipient_account_id",
            sa.UUID(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform_user_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("dismissed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("request_id", "recipient_account_id"),
    )


def downgrade() -> None:
    op.drop_table("github_access_request_deliveries")
    op.drop_index(
        "uq_github_access_request_open_thread_asker_agent",
        table_name="github_access_requests",
    )
    op.drop_index("ix_github_access_requests_waiting", table_name="github_access_requests")
    op.drop_table("github_access_requests")
