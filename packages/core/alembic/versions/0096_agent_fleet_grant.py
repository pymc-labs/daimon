"""Add finite per-agent fleet authority to immutable backend revisions.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0096_agent_fleet_grant"
down_revision: str | None = "0095_turn_outcome_worker_origin"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "agent_backend_revision", sa.Column("fleet_grant", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("agent_backend_revision", "fleet_grant")
