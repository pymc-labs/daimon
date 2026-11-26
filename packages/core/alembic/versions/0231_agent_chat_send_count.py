"""Count each agent-chat send, and the send whose turn was recorded.

`usage_reconcile_progress.agent_chat_sends` is incremented before every agent-chat send,
and `agent_chat_recorded_send` is set to that send's number once its turn is recorded.
Both are written only under the session's mutation fence. A person's send names them to
the identity relay only when the two match, so a send that may have been accepted
without its turn being recorded counts as another turn that may still be running.

Both columns are nullable with no default. A session with no count, including every row
the older release writes, counts as having an unrecorded send, which names nobody.

Downgrading drops the columns and their values. After an upgrade again they read NULL,
which names nobody until a send is recorded.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0231_agent_chat_send_count"
down_revision: str | None = "0230_agent_chat_signed_in"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "usage_reconcile_progress",
        sa.Column("agent_chat_sends", sa.Integer(), nullable=True),
    )
    op.add_column(
        "usage_reconcile_progress",
        sa.Column("agent_chat_recorded_send", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("usage_reconcile_progress", "agent_chat_recorded_send")
    op.drop_column("usage_reconcile_progress", "agent_chat_sends")
