"""The channel an agent's coding-tool token was minted in.

A token minted from the setup panel in a channel the agent is pinned to, or
in a sealed channel, records that channel and its platform; its calls then run
inside it (pins, seals, environment and channel budget). Every existing row
and every token minted anywhere else carries neither, and behaves as before.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0036_mcp_token_channels"
down_revision: str | None = "0035_channel_admins"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("mcp_tokens", sa.Column("platform", sa.Text(), nullable=True))
    op.add_column("mcp_tokens", sa.Column("channel_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_mcp_tokens_channel",
        "mcp_tokens",
        "(platform IS NULL) = (channel_id IS NULL)",
    )


def downgrade() -> None:
    # Destructive: a bound token becomes an ordinary agent key, outside every channel.
    op.drop_constraint("ck_mcp_tokens_channel", "mcp_tokens", type_="check")
    op.drop_column("mcp_tokens", "channel_id")
    op.drop_column("mcp_tokens", "platform")
