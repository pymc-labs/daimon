"""Remember the access needed and who approved a pending GitHub connection.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0071_github_request_decisions"
down_revision: str | None = "0070_github_access_wakes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "github_access_requests",
        sa.Column("required_ability", sa.Text(), nullable=False, server_default="read"),
    )
    op.add_column(
        "github_access_requests",
        sa.Column(
            "approved_by_account_id",
            sa.UUID(),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
        ),
    )


def downgrade() -> None:
    op.drop_column("github_access_requests", "approved_by_account_id")
    op.drop_column("github_access_requests", "required_ability")
