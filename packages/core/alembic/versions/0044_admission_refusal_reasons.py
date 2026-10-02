"""Record a protected, pinned or isolated refusal as its own turn outcome reason.

Until now these were stored as `admission_denied`, alongside the invoker
allowlist, so turn outcomes could not tell them apart. Rows written before
this migration keep `admission_denied`.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0044_admission_refusal_reasons"
down_revision: str | None = "0043_agent_admin_standing"
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
)
_ADDED = (
    "admission_channel_protected",
    "admission_agent_pinned_elsewhere",
    "admission_channel_isolated",
)
OUTCOME_REASONS = (*_OUTCOME_REASONS_BEFORE, *_ADDED)


def _replace_outcome_reasons(reasons: tuple[str, ...]) -> None:
    op.drop_constraint("ck_turn_outcomes_reason", "turn_outcomes", type_="check")
    values = ", ".join(f"'{reason}'" for reason in reasons)
    op.create_check_constraint("ck_turn_outcomes_reason", "turn_outcomes", f"reason IN ({values})")


def upgrade() -> None:
    _replace_outcome_reasons(OUTCOME_REASONS)


def downgrade() -> None:
    # The older constraint has no room for these, so they fold back into the
    # generic refusal they were recorded as before.
    added = ", ".join(f"'{reason}'" for reason in _ADDED)
    op.execute(f"UPDATE turn_outcomes SET reason = 'admission_denied' WHERE reason IN ({added})")
    _replace_outcome_reasons(_OUTCOME_REASONS_BEFORE)
