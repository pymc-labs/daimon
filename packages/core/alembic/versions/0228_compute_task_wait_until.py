"""compute_task_wait_until: the scheduler leaves an inline command while its caller waits.

Every command on a cloud computer is a task. An inline call waits for its task and
reports the result itself. `wait_until` is the time that caller stops waiting, plus
a short gap. Before it, the scheduler does not poll the task, so the conversation
does not get the same result twice: once in the tool result and once as a message.
After it, the scheduler polls the task and posts the message when it ends.

The column is nullable. A row with no value, which includes every row written before
this migration and every row the older release writes, is polled as before.

Downgrading drops the column. The scheduler then polls every active task, and a
command that ends while its caller waits can be reported twice.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0228_compute_task_wait_until"
down_revision: str | None = "0227_turn_origin_own_dm_reach"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "compute_tasks", sa.Column("wait_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("compute_tasks", "wait_until")
