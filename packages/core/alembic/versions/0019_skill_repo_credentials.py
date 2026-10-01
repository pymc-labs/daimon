"""agent_skill_repo_credentials — skill-repo tokens, separate from the working repo.

An agent has exactly one working repo (`agent_repo_binding`, PK
(tenant_id, agent_id)) but may import skills from any number of repos, so the
skill-repo token cannot live on that row: enrolling a skill repo would
silently re-point the repo the agent clones. This table's PK carries
`repo_url` as a third column, which is the whole point of it existing.

`repo_url` is stored in the canonical `owner/repo` form the binding store
already normalizes to, so a reverse lookup by repo matches.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0019_skill_repo_credentials"
down_revision: str | None = "0018_posted_controls_requests"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agent_skill_repo_credentials",
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False, server_default=""),
        sa.Column("ma_secret_ref", sa.Text(), nullable=False),
        sa.Column("proof_kind", sa.Text(), nullable=True),
        sa.Column("proof_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proof_account_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "agent_id", "repo_url", name="pk_agent_skill_repo_credentials"
        ),
    )
    op.create_index(
        "ix_agent_skill_repo_credentials_tenant_repo",
        "agent_skill_repo_credentials",
        ["tenant_id", "repo_url"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_skill_repo_credentials_tenant_repo", table_name="agent_skill_repo_credentials"
    )
    op.drop_table("agent_skill_repo_credentials")
