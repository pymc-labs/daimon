"""Record failed answer delivery separately from agent completion.

downgrade: destructive
"""

from alembic import op

revision: str = "0077_delivery_failure_outcome"
down_revision: str | None = "0076_github_connect_followup"
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
    "admission_channel_protected",
    "admission_agent_pinned_elsewhere",
    "admission_channel_isolated",
    "admission_denied",
    "admission_concurrency_shed",
    "missing_config",
    "resolver_miss",
    "session_preparation_failed",
    "session_busy",
    "session_agent_mismatch",
    "unknown",
)
OUTCOME_REASONS = (*_OUTCOME_REASONS_BEFORE, "delivery_failed")


def _replace_outcome_reasons(reasons: tuple[str, ...]) -> None:
    op.drop_constraint("ck_turn_outcomes_reason", "turn_outcomes", type_="check")
    values = ", ".join(f"'{reason}'" for reason in reasons)
    op.create_check_constraint("ck_turn_outcomes_reason", "turn_outcomes", f"reason IN ({values})")


def upgrade() -> None:
    _replace_outcome_reasons(OUTCOME_REASONS)


def downgrade() -> None:
    # Keep these as failures under the older vocabulary, rather than calling
    # an answer that never reached its recipient completed.
    op.execute("UPDATE turn_outcomes SET reason = 'unknown' WHERE reason = 'delivery_failed'")
    _replace_outcome_reasons(_OUTCOME_REASONS_BEFORE)
