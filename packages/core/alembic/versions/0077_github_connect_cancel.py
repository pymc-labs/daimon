"""Keep canceled GitHub OAuth tokens until revocation succeeds.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0077_github_connect_cancel"
down_revision: str | None = "0076_github_connect_followup"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("github_connect_flows", sa.Column("cancelled_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("github_connect_flows", "cancelled_at")
