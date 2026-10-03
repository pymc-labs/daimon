"""Keep the responder of live ordinary threads when defaults change.

Older sessions without a recorded agent name use their current channel or
server routing name. The concrete MA agent id remains the authority.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0048_opened_thread_bindings"
down_revision: str | None = "0047_turn_origin_external"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint("ck_thread_agent_bindings_kind", "thread_agent_bindings", type_="check")
    op.create_check_constraint(
        "ck_thread_agent_bindings_kind",
        "thread_agent_bindings",
        "kind IN ('setup', 'handoff', 'opened')",
    )
    op.execute(
        sa.text(
            """
            INSERT INTO thread_agent_bindings
                (tenant_id, platform, parent_channel_id, thread_id, kind,
                 responder_ma_agent_id, responder_name, creator_account_id)
            SELECT DISTINCT ON (s.tenant_id, s.platform,
                                COALESCE(o.channel_id, s.channel_id), s.thread_id)
                s.tenant_id, s.platform, COALESCE(o.channel_id, s.channel_id),
                s.thread_id, 'opened',
                s.ma_agent_id,
                COALESCE(NULLIF(s.effective_config->>'agent_name', ''),
                         NULLIF(c.agent_name, ''), NULLIF(t.agent_name, ''),
                         s.ma_agent_id),
                s.account_id
            FROM thread_sessions AS s
            LEFT JOIN channel_config AS c
                ON c.tenant_id = s.tenant_id AND c.channel_id = s.channel_id
            LEFT JOIN tenant_config AS t ON t.tenant_id = s.tenant_id
            LEFT JOIN LATERAL (
                SELECT channel_id FROM turn_outcomes
                WHERE tenant_id = s.tenant_id AND platform = s.platform
                  AND thread_id = s.thread_id AND agent_id = s.ma_agent_id
                  AND channel_id IS NOT NULL
                ORDER BY started_at ASC LIMIT 1
            ) AS o ON true
            WHERE s.status = 'live'
              AND s.ma_agent_id IS NOT NULL
              AND s.channel_id IS NOT NULL
              AND s.thread_id <> s.channel_id
              AND s.thread_id NOT LIKE 'dm:%'
            ORDER BY s.tenant_id, s.platform,
                     COALESCE(o.channel_id, s.channel_id), s.thread_id,
                     s.created_at ASC, s.id ASC
            ON CONFLICT ON CONSTRAINT uq_thread_agent_bindings_location DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM thread_agent_bindings WHERE kind = 'opened'"))
    op.drop_constraint("ck_thread_agent_bindings_kind", "thread_agent_bindings", type_="check")
    op.create_check_constraint(
        "ck_thread_agent_bindings_kind", "thread_agent_bindings", "kind IN ('setup', 'handoff')"
    )
