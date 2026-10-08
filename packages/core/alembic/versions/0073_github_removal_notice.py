"""Queue private admin notice for GitHub-side installation removal.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0073_github_removal_notice"
down_revision: str | None = "0072_github_expiry_notice"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "github_removal_notices",
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.Text(), nullable=False),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("tenant_id", "installation_id"),
    )


def downgrade() -> None:
    op.drop_table("github_removal_notices")
