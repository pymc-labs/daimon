"""Add single-use GitHub connection invitations and browser flows.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0049_github_connect"
down_revision: str | None = "0048_github_access_foundation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("github_issued_tokens", sa.Column("github_user_id", sa.BigInteger()))
    op.create_table(
        "github_connect_invitations",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "requester_account_id",
            sa.UUID(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_label", sa.Text(), nullable=False),
        sa.Column("requester_label", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "github_connect_flows",
        sa.Column("state_hash", sa.Text(), primary_key=True),
        sa.Column(
            "invitation_hash",
            sa.Text(),
            sa.ForeignKey("github_connect_invitations.token_hash", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cookie_hash", sa.Text(), nullable=False),
        sa.Column("encrypted_verifier", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_user_token", sa.LargeBinary()),
        sa.Column("github_user_id", sa.BigInteger()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("github_issued_tokens", "github_user_id")
    op.drop_table("github_connect_flows")
    op.drop_table("github_connect_invitations")
