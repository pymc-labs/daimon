"""Queue tenant-admin notices when an installation gains a repository.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0054_github_new_repo_notices"
down_revision: str | None = "0053_agent_avatars"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "github_new_repo_notices",
        sa.Column(
            "tenant_id",
            sa.UUID(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("tenant_id", "installation_id", "repo_full_name"),
    )


def downgrade() -> None:
    op.drop_table("github_new_repo_notices")
