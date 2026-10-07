"""Record what a turn posts, so an agent can tidy its own replies and cards.

`agent_posted_messages` gains three columns. `source` says which path posted
the row: `tool` (send_message/create_thread, every existing row), `turn` (a
status card, answer or notice the Discord adapter posted for a turn) or
`auto_thread` (a thread the adapter opened from a mention).
`requester_platform_user_id` is the person whose message started that turn or
opened that thread; `turn_card_intent_id` is the turn that posted a `turn` row,
so the tidy tools can refuse a turn that is still running.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050_turn_post_ownership"
down_revision: str | None = "0049_github_connect"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "agent_posted_messages",
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'tool'")),
    )
    op.add_column("agent_posted_messages", sa.Column("requester_platform_user_id", sa.Text()))
    op.add_column(
        "agent_posted_messages",
        sa.Column("turn_card_intent_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_check_constraint(
        "ck_agent_posted_messages_source",
        "agent_posted_messages",
        "source IN ('tool', 'turn', 'auto_thread')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_posted_messages_source", "agent_posted_messages", type_="check")
    op.drop_column("agent_posted_messages", "turn_card_intent_id")
    op.drop_column("agent_posted_messages", "requester_platform_user_id")
    op.drop_column("agent_posted_messages", "source")
