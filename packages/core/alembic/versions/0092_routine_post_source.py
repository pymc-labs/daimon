"""Record a routine's result as the agent's post: `source='routine'`.

`agent_posted_messages.source` gains `routine`, a result the chat adapter
posted for a routine. The row names the routine's agent and, as requester,
its creator, so a reply to the result can start a turn and only the creator
or an admin can have it tidied.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0092_routine_post_source"
down_revision: str | None = "0091_tenant_id_prefix_unique"
branch_labels: str | None = None
depends_on: str | None = None

_NAME = "ck_agent_posted_messages_source"
_TABLE = "agent_posted_messages"


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(
        _NAME, _TABLE, "source IN ('tool', 'turn', 'auto_thread', 'routine')"
    )


def downgrade() -> None:
    # The old constraint has no place for these rows; their results can no longer be tidied.
    op.execute("DELETE FROM agent_posted_messages WHERE source = 'routine'")
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, "source IN ('tool', 'turn', 'auto_thread')")
