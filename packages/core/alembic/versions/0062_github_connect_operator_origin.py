"""Mark GitHub connection links issued by an operator.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0062_github_connect_operator_origin"
down_revision: str | None = "0061_github_agent_connect"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "github_connect_invitations",
        sa.Column("operator_issued", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("github_connect_invitations", "operator_issued")
