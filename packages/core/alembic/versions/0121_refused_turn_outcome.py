"""Record a turn the model refused as its own turn outcome reason.

A refusal ends a Managed Agents turn with a plain `end_turn` idle, so these
turns were stored as `completed`. Rows written before this migration keep it.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0121_refused_turn_outcome"
down_revision: str | None = "0120_mcp_token_surface"
branch_labels: str | None = None
depends_on: str | None = None

_OUTCOME_REASONS_BEFORE = (
    "completed",
    "interrupted",
    "interrupt_timeout",
    "connection_lost",
    "upstream",
    "rate_limited",
    "session_terminated",
    "mcp_degraded_empty",
    "retrying_unsettled",
    "requires_action",
    "ceiling",
    "recovery_cancelled",
    "recovery_failed",
    "reducer_bug",
    "admission_balance_depleted",
    "admission_cap_exceeded",
    "admission_channel_budget_exceeded",
    "admission_denied",
    "admission_concurrency_shed",
    "missing_config",
    "resolver_miss",
    "session_preparation_failed",
    "session_busy",
    "session_agent_mismatch",
    "unknown",
    "admission_channel_protected",
    "admission_agent_pinned_elsewhere",
    "admission_channel_isolated",
)
OUTCOME_REASONS = (*_OUTCOME_REASONS_BEFORE, "refused")


def _replace_outcome_reasons(reasons: tuple[str, ...]) -> None:
    op.drop_constraint("ck_turn_outcomes_reason", "turn_outcomes", type_="check")
    values = ", ".join(f"'{reason}'" for reason in reasons)
    op.create_check_constraint("ck_turn_outcomes_reason", "turn_outcomes", f"reason IN ({values})")


def upgrade() -> None:
    _replace_outcome_reasons(OUTCOME_REASONS)


def downgrade() -> None:
    # The older constraint has no room for it; a refusal is a failed upstream reply.
    op.execute("UPDATE turn_outcomes SET reason = 'upstream' WHERE reason = 'refused'")
    _replace_outcome_reasons(_OUTCOME_REASONS_BEFORE)
