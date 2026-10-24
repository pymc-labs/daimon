"""Mark a turn origin whose sender is from another organisation.

`turn_origins.sender_foreign` is set from the Teams adapter's `TeamsInbound.is_foreign`.
Teams opens no 1:1 chat with such a person, so the per-turn words, the here card and the
MCP tools that point to a 1:1 chat read it to say who to ask instead. Existing rows
default to false, and an origin lives two hours at most.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0111_turn_origin_foreign_sender"
down_revision: str | None = "0110_microsoft_accounts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "turn_origins",
        sa.Column("sender_foreign", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("turn_origins", "sender_foreign")
