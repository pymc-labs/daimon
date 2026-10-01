"""Give the shared turn marker a channel, so a Slack boot sweep can find the frozen status card.

Revision ID: 0011_active_turn_channel
Revises: 0010_agent_mcp_credentials
Create Date: 2026-08-29

downgrade: safe

Nullable with no backfill: a row with no marker has no channel to record, so
every existing row is legitimately NULL here. NULL is also correct FOREVER for
Discord, not just at migration time -- a Discord message id is globally
addressable, so the Discord adapter never writes this column and its rows stay
NULL for the life of the table.

First-boot consequence: a Slack orphan row that already exists when
this migration lands carries NULL in this column too, since it predates the
column entirely. The boot sweep must clear that row without attempting a
`chat_update` call -- there is no channel to address.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_active_turn_channel"
down_revision: str | None = "0010_agent_mcp_credentials"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "thread_sessions",
        sa.Column("active_turn_channel_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("thread_sessions", "active_turn_channel_id")
