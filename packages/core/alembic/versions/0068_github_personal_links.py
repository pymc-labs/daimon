"""Browser-bound, single-use personal GitHub link invitations.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0068_github_personal_links"
down_revision: str | None = "0067_github_access_requests"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "github_personal_link_intents",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column(
            "account_id",
            sa.UUID(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_user_id", sa.Text(), nullable=False),
        sa.Column("platform_workspace_id", sa.Text(), nullable=False),
        sa.Column("platform_state", sa.Text()),
        sa.Column("github_state", sa.Text()),
        sa.Column("browser_cookie_hash", sa.Text()),
        sa.Column("encrypted_verifier", sa.LargeBinary()),
        sa.Column("phase", sa.Text(), nullable=False, server_default="new"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("platform IN ('discord', 'slack')"),
        sa.CheckConstraint("phase IN ('new', 'platform', 'github', 'used')"),
    )
    op.create_index(
        "ix_github_personal_link_intents_expires_at",
        "github_personal_link_intents",
        ["expires_at"],
    )
    op.create_index(
        "uq_github_personal_link_intents_platform_state",
        "github_personal_link_intents",
        ["platform_state"],
        unique=True,
    )
    op.create_index(
        "uq_github_personal_link_intents_github_state",
        "github_personal_link_intents",
        ["github_state"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_github_personal_link_intents_github_state",
        table_name="github_personal_link_intents",
    )
    op.drop_index(
        "uq_github_personal_link_intents_platform_state",
        table_name="github_personal_link_intents",
    )
    op.drop_index(
        "ix_github_personal_link_intents_expires_at",
        table_name="github_personal_link_intents",
    )
    op.drop_table("github_personal_link_intents")
