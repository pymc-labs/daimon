"""compute_started_by_wake: the wake turns that acted on a cloud computer.

When an admin lets the agent continue on a cloud computer, the turn that a finished
command wakes may run the next command, write a file or stop the machine.
`compute_sessions.wake_turns` lists the wake turns that did so since the owner last
ran a command or wrote a file there. `DAIMON_COMPUTE__WAKE_RUN_LIMIT` bounds its
length. It changes only under the session row lock, so no clock decides the count.

The column is a JSON list, NOT NULL, with the empty list as its server default, as
`extension_keys` is. The older release inserts and updates sessions without naming
it, so every row it writes stays valid and its list stays as it was.

Downgrading drops the column. Every list then starts again from empty.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0229_compute_started_by_wake"
down_revision: str | None = "0228_compute_task_wait_until"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "compute_sessions",
        sa.Column(
            "wake_turns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("compute_sessions", "wake_turns")
