"""isolated_fresh_start: start afresh the live sessions `isolate_context` made.

The removed option recorded `context_isolated` in `effective_config` on sessions
that mount only a relay. Updated in place, such a session would keep running
without daimon-mcp or memory. A fresh start replaces it on its next turn and
carries nothing. Data only.

downgrade: safe
"""

from __future__ import annotations

from alembic import op

revision: str = "0146_isolated_fresh_start"
down_revision: str | None = "0145_turn_origin_message"
branch_labels: str | None = None
depends_on: str | None = None


_ISOLATED = "status = 'live' AND effective_config ->> 'context_isolated' = 'true'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE thread_sessions SET fresh_start_requested_at = now() "
        f"WHERE {_ISOLATED} AND fresh_start_requested_at IS NULL"
    )


def downgrade() -> None:
    # Older code replaces these sessions itself, from `context_isolated`.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"UPDATE thread_sessions SET fresh_start_requested_at = NULL WHERE {_ISOLATED}")
