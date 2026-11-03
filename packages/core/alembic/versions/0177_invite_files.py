"""Per-invite file attachments: whether a visitor may attach files in web chat.

`web_invites.allow_files` is false for every existing invite, so visitors keep chatting
without files until an admin turns them on. Downgrading drops the setting.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0177_invite_files"
down_revision: str | None = "0176_routine_names"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "web_invites",
        sa.Column("allow_files", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("web_invites", "allow_files")
