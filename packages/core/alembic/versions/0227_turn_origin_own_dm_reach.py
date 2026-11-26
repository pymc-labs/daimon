"""turn_origins: own-DM reach and its per-turn latch.

`own_dm_reach` is set when a turn is admitted: true only for an interactive owner DM
turn and for a routine fired by the scheduler's owner-DM path. `reach_read` is set when
the turn read a limited room through that reach; from then on the turn's posts and direct
messages go to its own DM only. Both default to false, so an origin written before this
migration has no reach.
Downgrading drops both; no turn then reads a limited room from a DM.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0227_turn_origin_own_dm_reach"
down_revision: str | None = "0226_compute_tool_names"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for column in ("own_dm_reach", "reach_read"):
        op.add_column(
            "turn_origins",
            sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )


def downgrade() -> None:
    op.drop_column("turn_origins", "reach_read")
    op.drop_column("turn_origins", "own_dm_reach")
