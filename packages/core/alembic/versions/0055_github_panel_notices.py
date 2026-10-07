"""Track one new-repo card claim and optional invitation preselection.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0055_github_panel_notices"
down_revision: str | None = "0054_github_token_audit_versions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("github_connect_invitations", sa.Column("preselected_repo_full_name", sa.Text()))
    op.add_column("github_new_repo_notices", sa.Column("claimed_at", sa.DateTime(timezone=True)))
    op.add_column("github_new_repo_notices", sa.Column("dismissed_at", sa.DateTime(timezone=True)))
    op.create_table(
        "agent_github_grant_drafts",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("baseline_access", sa.Text()),
        sa.Column("ceiling_access", sa.Text()),
        sa.Column("is_working_repo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "granted_by_account_id", sa.UUID(), sa.ForeignKey("accounts.id", ondelete="SET NULL")
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "repo_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "repo_id"],
            ["tenant_github_repos.tenant_id", "tenant_github_repos.repo_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("operation IN ('upsert', 'remove')"),
    )


def downgrade() -> None:
    op.drop_table("agent_github_grant_drafts")
    op.drop_column("github_new_repo_notices", "dismissed_at")
    op.drop_column("github_new_repo_notices", "claimed_at")
    op.drop_column("github_connect_invitations", "preselected_repo_full_name")
