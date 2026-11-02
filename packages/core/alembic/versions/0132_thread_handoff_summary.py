"""thread_handoff_summary: a cross-provider hand-off's summary, waiting for its first turn.

A hand-off to an agent on another provider can't carry the conversation itself,
so the handing-off agent writes a short summary. It waits here, on the thread's
binding, until the destination's first turn in that thread takes it (once) as
quoted, untrusted material. A replaced or released binding clears it.

Additive and nullable: existing bindings carry nothing. Downgrading drops the
column; a summary still waiting is then lost and that first turn starts without it.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0132_thread_handoff_summary"
down_revision: str | None = "0131_usage_obs_tenant_idx"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("thread_agent_bindings", sa.Column("handoff_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("thread_agent_bindings", "handoff_summary")
