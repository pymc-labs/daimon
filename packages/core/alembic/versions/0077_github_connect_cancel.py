"""Keep canceled GitHub OAuth tokens until revocation succeeds.

downgrade: destructive
Refused while cancelled flows retain tokens pending revocation.
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
    # Hold writers out until the column is gone: a concurrent Cancel must not
    # create pending revocation work after the check but before DROP COLUMN.
    op.execute("LOCK TABLE github_connect_flows IN SHARE ROW EXCLUSIVE MODE")
    pending = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM github_connect_flows "
            "WHERE cancelled_at IS NOT NULL AND encrypted_user_token IS NOT NULL)"
        )
    )
    if pending:
        raise RuntimeError(
            "Cannot downgrade GitHub Connect cancellation while cancelled flows "
            "still hold tokens pending revocation"
        )
    op.drop_column("github_connect_flows", "cancelled_at")
