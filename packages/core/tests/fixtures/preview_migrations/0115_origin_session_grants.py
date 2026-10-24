"""Bind live origins to isolated caller-session credentials.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0115_origin_session_grants"
down_revision: str | None = "0114_turn_attendance"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("turn_origins", sa.Column("chat_session_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("turn_origins", "chat_session_id")
