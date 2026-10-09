"""Record one expiry notice for an unfinished GitHub request.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0072_github_expiry_notice"
down_revision: str | None = "0071_github_request_decisions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "github_access_requests",
        sa.Column("expiry_notice_sent_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("github_access_requests", "expiry_notice_sent_at")
