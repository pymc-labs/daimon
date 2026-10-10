"""Remember the repo named when a Connect GitHub link was offered.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0081_github_connect_requested_repo"
down_revision: str | None = "0080_github_grant_proposals"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("github_connect_invitations", sa.Column("requested_repo", sa.Text()))
    op.add_column("github_connect_click_intents", sa.Column("requested_repo", sa.Text()))


def downgrade() -> None:
    op.drop_column("github_connect_click_intents", "requested_repo")
    op.drop_column("github_connect_invitations", "requested_repo")
