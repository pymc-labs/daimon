"""Remember why a saved-key session must restart on its next turn.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0069_github_key_restart_notice"
down_revision: str | None = "0068_github_personal_links"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "thread_sessions",
        sa.Column(
            "github_key_restart_notice",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("thread_sessions", "github_key_restart_notice")
