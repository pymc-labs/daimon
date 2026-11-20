"""A routine's pending result carries the hold its fire ran under.

`routines.delivery_hold` is JSONB. NULL is a result queued before this column (or one
with no result): delivery keeps its earlier logic. Otherwise it is `{"held": false}` or
`{"held": true, "channel_id": ..., "client": ..., "relay": ...}`: the hold of the fire
that produced the pending result, written in the same UPDATE as `delivery_payload`.
Additive only.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0221_routine_delivery_hold"
down_revision: str | None = "0220_discord_audience_facts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "routines",
        sa.Column("delivery_hold", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("routines", "delivery_hold")
