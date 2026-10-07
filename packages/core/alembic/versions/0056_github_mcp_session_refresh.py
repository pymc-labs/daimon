"""Keep enough state to refresh GitHub App tokens in MCP sessions.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0056_github_mcp_session_refresh"
down_revision: str | None = "0055_github_token_audit_versions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "github_app_session_vaults",
        sa.Column("is_mcp", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("github_app_session_vaults", sa.Column("agent_id", sa.UUID()))
    op.add_column(
        "github_app_session_vaults",
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id", ondelete="SET NULL")),
    )
    op.add_column("github_app_session_vaults", sa.Column("repo_urls", JSONB()))
    op.add_column("github_app_session_vaults", sa.Column("repo_resource_ids", JSONB()))
    op.create_index(
        "ix_github_app_session_vaults_mcp_open",
        "github_app_session_vaults",
        ["is_mcp", "closed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_github_app_session_vaults_mcp_open", table_name="github_app_session_vaults")
    op.drop_column("github_app_session_vaults", "repo_resource_ids")
    op.drop_column("github_app_session_vaults", "repo_urls")
    op.drop_column("github_app_session_vaults", "account_id")
    op.drop_column("github_app_session_vaults", "agent_id")
    op.drop_column("github_app_session_vaults", "is_mcp")
