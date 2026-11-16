"""active_turn_deadline: each in-flight turn's own deadline on the thread marker.

`thread_sessions` gains the nullable `active_turn_deadline`, written with the
rest of the active-turn marker. A turn may now run longer than the standard 45
minutes (the answer time limit allows 90 and 180), so the stale-marker check
reads the turn's own deadline. NULL, which every existing row and every turn
without a limit of its own carries, keeps the 45-minute window.

It also gains the nullable `model_marks`: the model and effort emoji a turn
last showed in the thread, so a restart does not show the same pair again.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0206_active_turn_deadline"
down_revision: str | None = "0205_web_search_usage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "thread_sessions",
        sa.Column("active_turn_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "thread_sessions",
        sa.Column("model_marks", postgresql.ARRAY(sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("thread_sessions", "model_marks")
    op.drop_column("thread_sessions", "active_turn_deadline")
