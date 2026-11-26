"""Record whether an agent-chat session's driver is a person signed in to the hub.

`usage_reconcile_progress.agent_chat_signed_in` is written before every send or
resume on an agent-chat session: true only when a person signed in to the MCP hub
with their platform login drives an otherwise idle session, false for any key. The
identity relay names that person to a remote MCP server that opted in.

The column is nullable with no default. The older release writes its rows without
naming it, and NULL reads as false, so nothing it writes names anyone.

Downgrading drops the column and its values. After an upgrade again it reads NULL,
which names nobody, until the next send records it.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0230_agent_chat_signed_in"
down_revision: str | None = "0229_compute_started_by_wake"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "usage_reconcile_progress",
        sa.Column("agent_chat_signed_in", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("usage_reconcile_progress", "agent_chat_signed_in")
