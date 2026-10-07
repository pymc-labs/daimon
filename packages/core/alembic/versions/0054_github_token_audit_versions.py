"""Record grant and authorization versions on GitHub token audit rows.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0054_github_token_audit_versions"
down_revision: str | None = "0053_github_new_repo_notices"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("security_audit_events", sa.Column("github_grant_versions", JSONB()))
    op.add_column("security_audit_events", sa.Column("github_turn_origin_id", sa.UUID()))
    op.add_column("github_issued_tokens", sa.Column("superseded_at", sa.DateTime(timezone=True)))
    op.add_column("github_issued_tokens", sa.Column("revoke_after", sa.DateTime(timezone=True)))
    op.add_column(
        "github_issued_tokens",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "github_app_session_vaults",
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vault_id", sa.Text(), nullable=False),
        sa.Column("is_unmapped", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("github_app_session_vaults")
    op.drop_column("github_issued_tokens", "created_at")
    op.drop_column("github_issued_tokens", "revoke_after")
    op.drop_column("github_issued_tokens", "superseded_at")
    op.drop_column("security_audit_events", "github_turn_origin_id")
    op.drop_column("security_audit_events", "github_grant_versions")
