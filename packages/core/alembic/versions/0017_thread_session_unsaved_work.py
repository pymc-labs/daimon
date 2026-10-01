"""The caller's answer to the uncommitted-work question, held until it is used.

Revision ID: 0017_thread_session_unsaved_work
Revises: 0016_session_continuity

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_thread_session_unsaved_work"
down_revision: str | None = "0016_session_continuity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The question is asked in one turn and answered in the next, so the answer
    # has to survive between them on the caller's own row. Untyped Text with no
    # CHECK, like `status` and `transfer_kind`: the vocabulary
    # ('copy' | 'leave') is pinned by `stores.domain.UnsavedWorkChoice`, and a
    # value outside it raises on read rather than needing a lock to widen.
    op.add_column("thread_sessions", sa.Column("pending_unsaved_work", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("thread_sessions", "pending_unsaved_work")
