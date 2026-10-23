"""thread_binding_owner_kind: a thread handed to its owner's private copy.

A person may hand a thread only they are in to an agent they own (a copy they made),
even before it answers anywhere. That binding (`kind='owner'`) answers only its
creator: anyone else who speaks in the thread is told it is someone's private copy,
and nothing of theirs runs on it.

Downgrading deletes owner bindings first (their threads then answer as the channel
does, as before the hand-off); the CHECK narrows back.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0131_thread_binding_owner_kind"
down_revision: str | None = "0128_fleet_run_end_notice"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Postgres cannot widen a CHECK in place.
    op.drop_constraint("ck_thread_agent_bindings_kind", "thread_agent_bindings", type_="check")
    op.create_check_constraint(
        "ck_thread_agent_bindings_kind",
        "thread_agent_bindings",
        "kind IN ('setup', 'handoff', 'owner')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(sa.text("DELETE FROM thread_agent_bindings WHERE kind = 'owner'"))
    op.drop_constraint("ck_thread_agent_bindings_kind", "thread_agent_bindings", type_="check")
    op.create_check_constraint(
        "ck_thread_agent_bindings_kind",
        "thread_agent_bindings",
        "kind IN ('setup', 'handoff')",
    )
