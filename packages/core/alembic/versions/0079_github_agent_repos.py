"""Let a connected GitHub repo belong to one agent.

`tenant_github_repos` gains a surrogate key and `scope_agent_id` (null =
server-wide), unique on (tenant_id, repo_id, scope_agent_id) with nulls not
distinct. Grants and drafts can no longer reference one (tenant_id, repo_id)
row, so their foreign key moves to the tenant. Connect invitations remember
the target agent's Managed Agents id for the confirm-time check.

downgrade: destructive
Downgrading deletes every agent-scoped repo row and the grants that only it
covered, then restores the (tenant_id, repo_id) key.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0079_github_agent_repos"
down_revision: str | None = "0078_privacy_session_deletes"
branch_labels: str | None = None
depends_on: str | None = None

_GRANT_TABLES = ("agent_github_grants", "agent_github_grant_drafts")


def _drop_repo_foreign_keys() -> None:
    # 0048 and 0066 left these constraints to Postgres' default names.
    op.drop_constraint("agent_github_grants_tenant_id_repo_id_fkey", "agent_github_grants")
    op.drop_constraint(
        "agent_github_grant_drafts_tenant_id_repo_id_fkey", "agent_github_grant_drafts"
    )


def upgrade() -> None:
    _drop_repo_foreign_keys()
    for table in _GRANT_TABLES:
        op.create_foreign_key(
            f"{table}_tenant_id_fkey", table, "tenants", ["tenant_id"], ["id"], ondelete="CASCADE"
        )
    op.add_column(
        "tenant_github_repos",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.add_column("tenant_github_repos", sa.Column("scope_agent_id", postgresql.UUID(as_uuid=True)))
    op.drop_constraint("tenant_github_repos_pkey", "tenant_github_repos", type_="primary")
    op.create_primary_key("tenant_github_repos_pkey", "tenant_github_repos", ["id"])
    op.create_unique_constraint(
        "uq_tenant_github_repos_scope",
        "tenant_github_repos",
        ["tenant_id", "repo_id", "scope_agent_id"],
        postgresql_nulls_not_distinct=True,
    )
    op.add_column("github_connect_invitations", sa.Column("agent_ma_id", sa.Text()))
    op.add_column("github_connect_click_intents", sa.Column("agent_ma_id", sa.Text()))


def downgrade() -> None:
    op.drop_column("github_connect_click_intents", "agent_ma_id")
    op.drop_column("github_connect_invitations", "agent_ma_id")
    # Grants covered only by an agent-scoped row lose their repo.
    for table in _GRANT_TABLES:
        op.execute(
            f"""DELETE FROM {table} AS g
            WHERE NOT EXISTS (
                SELECT 1 FROM tenant_github_repos AS r
                WHERE r.tenant_id = g.tenant_id
                  AND r.repo_id = g.repo_id
                  AND r.scope_agent_id IS NULL
            )"""
        )
    op.execute("DELETE FROM tenant_github_repos WHERE scope_agent_id IS NOT NULL")
    op.drop_constraint("uq_tenant_github_repos_scope", "tenant_github_repos", type_="unique")
    op.drop_constraint("tenant_github_repos_pkey", "tenant_github_repos", type_="primary")
    op.create_primary_key(
        "tenant_github_repos_pkey", "tenant_github_repos", ["tenant_id", "repo_id"]
    )
    op.drop_column("tenant_github_repos", "scope_agent_id")
    op.drop_column("tenant_github_repos", "id")
    for table in _GRANT_TABLES:
        op.drop_constraint(f"{table}_tenant_id_fkey", table)
        op.create_foreign_key(
            f"{table}_tenant_id_repo_id_fkey",
            table,
            "tenant_github_repos",
            ["tenant_id", "repo_id"],
            ["tenant_id", "repo_id"],
            ondelete="CASCADE",
        )
