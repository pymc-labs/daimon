"""Content-free terminal diagnostics, keyed once per logical turn.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029_sys081_turn_outcomes"
down_revision = "0028_tenant_funding_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "turn_outcomes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE")),
        sa.Column("account_id", sa.UUID()),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text()),
        sa.Column("thread_id", sa.Text()),
        sa.Column("agent_id", sa.Text()),
        sa.Column("session_id", sa.Text()),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("recovered", sa.Boolean(), nullable=False),
        sa.Column("error_class", sa.Text()),
        sa.Column("release", sa.Text(), nullable=False),
        sa.Column("usage_refs", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "origin IN ('chat', 'routine', 'relay', 'handoff')", name="ck_turn_outcomes_origin"
        ),
        sa.CheckConstraint(
            "reason IN ('completed', 'interrupted', 'interrupt_timeout', "
            "'connection_lost', 'upstream', 'rate_limited', 'session_terminated', "
            "'mcp_degraded_empty', 'retrying_unsettled', 'requires_action', 'ceiling', "
            "'recovery_cancelled', 'recovery_failed', 'reducer_bug', "
            "'admission_balance_depleted', 'admission_cap_exceeded', 'admission_denied', "
            "'admission_concurrency_shed', 'missing_config', 'resolver_miss', "
            "'session_preparation_failed', 'session_busy', 'session_agent_mismatch', "
            "'unknown')",
            name="ck_turn_outcomes_reason",
        ),
        sa.CheckConstraint("duration_ms >= 0", name="ck_turn_outcomes_duration"),
    )
    op.create_index("ix_turn_outcomes_tenant_started", "turn_outcomes", ["tenant_id", "started_at"])


def downgrade() -> None:
    op.drop_table("turn_outcomes")
