"""turn_origins: whether the turn's thread is a public one.

`public_thread` is the platform's word, at the turn's start, that its thread is public
(a forum post and an announcement thread too). A signed MCP call reads the room's facts
for the place the answer goes to: a public thread with no fact of its own yet takes its
parent's, any other thread with none is narrow. It defaults to false, so an origin written
before this migration reads its new thread as narrow until the thread's own fact is stored.
Downgrading drops it; every such thread then reads as narrow.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0233_turn_origin_public_thread"
down_revision: str | None = "0232_agent_chat_session_grant"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "turn_origins",
        sa.Column("public_thread", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("turn_origins", "public_thread")
