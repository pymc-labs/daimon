"""Record an agent's request to archive the thread its turn runs in.

`turn_origins.archive_requested_at` is set by `archive_thread` once the call
has passed every check, when the thread is the one the turn runs in (Discord
refuses edits in an archived thread, so the turn could not finish its card).
The chat adapter reads it when the turn's run ends and archives the thread
after the turn's last post. The row is deleted with its origin.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0051_turn_origin_archive"
down_revision: str | None = "0050_turn_post_ownership"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("turn_origins", sa.Column("archive_requested_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("turn_origins", "archive_requested_at")
