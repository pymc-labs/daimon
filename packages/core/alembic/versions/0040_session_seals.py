"""Persist monotone session seals for locked handoff decisions.

Legacy NULL facts stay unknown until read from MA; unknown fails closed.
downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_session_seals"
down_revision: str | None = "0039_skill_uploads"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("thread_sessions", sa.Column("seal_ids", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("thread_sessions", "seal_ids")
