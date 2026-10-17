"""Agent-wide grants for an installation owner's covered repositories.

Also adds the per-agent grant fence row every grant writer bumps.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0111_github_owner_grants"
down_revision: str | None = "0110_github_link_pending"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "github_linked_connect_pending",
        sa.Column("requested_access", sa.Text(), nullable=False, server_default="read"),
    )
    op.create_check_constraint(
        "ck_github_linked_connect_pending_access",
        "github_linked_connect_pending",
        "requested_access IN ('read', 'write')",
    )
    op.create_table(
        "agent_github_owner_grants",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "installation_id",
            sa.BigInteger(),
            sa.ForeignKey("github_app_installations.installation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_login", sa.Text(), nullable=False),
        sa.Column("access", sa.Text(), nullable=False),
        sa.Column(
            "granted_by_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "installation_id"),
        sa.CheckConstraint(
            "access IN ('read', 'write')", name="ck_agent_github_owner_grants_access"
        ),
    )
    op.create_table(
        "agent_github_owner_exclusions",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "repo_full_name"),
    )
    op.create_table(
        "agent_github_grant_fences",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id"),
    )
    op.add_column(
        "github_issued_tokens",
        sa.Column("repo_names", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_github_linked_connect_pending_access", "github_linked_connect_pending")
    op.drop_column("github_linked_connect_pending", "requested_access")
    op.drop_column("github_issued_tokens", "repo_names")
    op.drop_table("agent_github_grant_fences")
    op.drop_table("agent_github_owner_exclusions")
    op.drop_table("agent_github_owner_grants")
