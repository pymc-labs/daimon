"""Retain explicit source-loss consent with agent and effective backend revisions.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0102_agent_backend_drop_consent"
down_revision: str | None = "0101_teams_message_admissions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "agent_backend_revision", sa.Column("drop_consent", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "channel_config_revision", sa.Column("agent_backend_ref", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("channel_config_revision", "agent_backend_ref")
    op.drop_column("agent_backend_revision", "drop_consent")
