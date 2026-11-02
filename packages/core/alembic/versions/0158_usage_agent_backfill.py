"""usage_agent_backfill: older usage rows name the agent whose session spent it.

Usage recorded before `usage_events.agent_id` existed has no agent, so per-agent
spend reads $0 for it. Each such row takes the agent its session belongs to, from
`thread_sessions` or `turn_outcomes` in the same tenant, when every source agrees on
one agent. Rows with no known agent (thread naming, classifiers) stay unattributed.
Downgrading cannot tell backfilled rows from recorded ones.

downgrade: unsupported
"""

from __future__ import annotations

from alembic import op

revision: str = "0158_usage_agent_backfill"
down_revision: str | None = "0157_web_node_tokens"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE usage_events u SET agent_id = s.agent_id
        FROM (
            SELECT tenant_id, session_id, min(agent_id) AS agent_id FROM (
                SELECT tenant_id, ma_session_id AS session_id, ma_agent_id AS agent_id
                FROM thread_sessions WHERE ma_agent_id IS NOT NULL
                UNION ALL
                SELECT tenant_id, session_id, agent_id FROM turn_outcomes
                WHERE tenant_id IS NOT NULL AND session_id IS NOT NULL AND agent_id IS NOT NULL
            ) pairs
            GROUP BY tenant_id, session_id
            HAVING count(DISTINCT agent_id) = 1
        ) s
        WHERE u.agent_id IS NULL AND u.tenant_id = s.tenant_id
            AND u.managed_session_id = s.session_id
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Backfilled agents cannot be told from recorded ones")
