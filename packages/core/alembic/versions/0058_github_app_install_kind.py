"""Separate legacy and new GitHub App installations and backfill authorizations.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0058_github_app_install_kind"
down_revision: str | None = "0057_github_mcp_session_refresh"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "github_app_installations",
        sa.Column("app", sa.Text(), nullable=False, server_default="legacy"),
    )
    op.create_check_constraint(
        "ck_github_app_installations_app",
        "github_app_installations",
        "app IN ('legacy', 'github_app')",
    )
    # Existing authorizations were confirmed without installing a cache row.
    # Preserve manually entered metadata while marking those rows as new-App
    # rows and rebuilding their repository set from existing authorizations.
    op.execute(
        """INSERT INTO github_app_installations
            (installation_id, account_login, account_id, repo_full_names, app)
        SELECT installation_id,
               MIN(split_part(repo_full_name, '/', 1)),
               MIN(owner_id),
               ARRAY_AGG(DISTINCT repo_full_name ORDER BY repo_full_name),
               'github_app'
        FROM tenant_github_repos
        GROUP BY installation_id
        ON CONFLICT (installation_id) DO UPDATE
        SET app = 'github_app',
            repo_full_names = EXCLUDED.repo_full_names,
            updated_at = now()"""
    )


def downgrade() -> None:
    op.drop_constraint("ck_github_app_installations_app", "github_app_installations")
    op.drop_column("github_app_installations", "app")
