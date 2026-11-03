"""Routine names: an optional title people give a routine.

`routines.name` is null for every existing routine; the web app shows the first line of the
prompt until one is set. Downgrading drops the names.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0176_routine_names"
down_revision: str | None = "0175_channel_name_status"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("routines", sa.Column("name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("routines", "name")
