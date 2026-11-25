"""compute_started_by_wake: the wake turn that started a command, if one did.

When an admin lets the agent continue on a cloud computer, the turn that a finished
command wakes may start the next command. `started_by_wake` holds that turn's id, and
stays empty for a command the owner started. The wake turns that started commands
since the owner's last one are counted against `DAIMON_COMPUTE__WAKE_RUN_LIMIT`.

The column is nullable. A row with no value, which includes every row written before
this migration and every row the older release writes, counts as the owner's.

Downgrading drops the column. Every command then counts as the owner's, so the count
of wake turns starts again from zero.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0229_compute_started_by_wake"
down_revision: str | None = "0228_compute_task_wait_until"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("compute_tasks", sa.Column("started_by_wake", sa.UUID(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("compute_tasks", "started_by_wake")
