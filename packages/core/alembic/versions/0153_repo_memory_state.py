"""Remember repository-memory state already delivered to a thread session.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision = "0153_repo_memory_state"
down_revision = "0152_repo_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("thread_sessions", sa.Column("repo_memory_sha256", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("thread_sessions", "repo_memory_sha256")
