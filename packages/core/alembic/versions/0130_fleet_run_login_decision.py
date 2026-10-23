"""fleet_run_login_decision: the run's one decision about which stored logins its workers get.

`fleet_run.login_decision` is null until the run's first worker spawn, then set once
(from null, never changed) to {login key: "share" | "skip"} by the run's own
requester's answer, or "skip" for every key when nobody answers in time. Workers
receive only keys the run decided to share, that the root froze as candidates, and
that still hold when they start. `fleet_run.login_question` is the open question the
chat adapter posts as a card (the keys by name, its deadline, the answers so far).
Additive; downgrading drops both, and runs then read "skip" for every key.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0130_fleet_run_login_decision"
down_revision: str | None = "0127_fleet_credential_sharing"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("fleet_run", sa.Column("login_decision", postgresql.JSONB(), nullable=True))
    op.add_column("fleet_run", sa.Column("login_question", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("fleet_run", "login_question")
    op.drop_column("fleet_run", "login_decision")
