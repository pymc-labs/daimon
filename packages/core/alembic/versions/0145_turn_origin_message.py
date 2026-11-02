"""turn_origin_message: the platform ids of the message that started a turn.

`turn_origins.platform_channel_id` is the channel, thread or chat the turn's message
can be read in through the platform's API, and `platform_message_id` that message.
Signed caller identities for remote MCP servers name both. NULL on turns no message
started and on rows an older process writes. Additive only.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0145_turn_origin_message"
down_revision: str | None = "0144_mcp_token_purpose"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("turn_origins", sa.Column("platform_channel_id", sa.Text(), nullable=True))
    op.add_column("turn_origins", sa.Column("platform_message_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("turn_origins", "platform_message_id")
    op.drop_column("turn_origins", "platform_channel_id")
