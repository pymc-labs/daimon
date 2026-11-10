"""long-job policy as feature rows: `long_jobs`, `long_jobs_max_usd`, `long_jobs_max_deadline`

Copies each `long_job_policy` row into `feature_values`: a tenant row becomes a workspace
value and an agent row an agent value. Enabled is `long_jobs` On (an agent row that is not
enabled writes Off, so it still overrides the workspace). A dollar or deadline maximum
becomes the largest step at or below it, only where that differs from the default (the
smallest step stands in for a value below every step). The policy tables stay, unread, so an
older build can still start. Each tenant's `feature_revision` is bumped. A downgrade removes
the rows this migration wrote.

Revision ID: 0190_long_job_features
Revises: 0189_workspace_feature_values

downgrade: safe
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0190_long_job_features"
down_revision: str | None = "0189_workspace_feature_values"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USD = ("20", "10", "5", "2", "1")
_DEADLINE = ("70", "60", "30", "15")
_KEYS = "('long_jobs', 'long_jobs_max_usd', 'long_jobs_max_deadline')"
_PLACE = "CASE WHEN agent_id IS NULL THEN 'workspace' ELSE 'agent' END, coalesce(agent_id, '')"


def _step(column: str, steps: Sequence[str]) -> str:
    """The largest step at or below `column`, or the smallest step."""
    whens = " ".join(f"WHEN {column} >= {step} THEN '{step}'" for step in steps[:-1])
    return f"CASE {whens} ELSE '{steps[-1]}' END"


def _insert(key: str, value: str, where: str) -> str:
    return (
        "INSERT INTO feature_values (tenant_id, tier, agent_id, key, value, surface) "
        f"SELECT tenant_id, {_PLACE}, '{key}', {value}, 'migration' "
        f"FROM long_job_policy WHERE {where} ON CONFLICT DO NOTHING"
    )


def _bump(source: str) -> str:
    return (
        "UPDATE tenants SET feature_revision = feature_revision + 1 "
        f"WHERE id IN (SELECT tenant_id FROM {source})"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_insert("long_jobs", "'on'", "enabled"))
    op.execute(_insert("long_jobs", "'off'", "NOT enabled AND agent_id IS NOT NULL"))
    for key, column, steps in (
        ("long_jobs_max_usd", "max_usd", _USD),
        ("long_jobs_max_deadline", "max_deadline_minutes", _DEADLINE),
    ):
        step = _step(column, steps)
        default = "5" if column == "max_usd" else steps[0]
        op.execute(_insert(key, step, f"{step} <> '{default}'"))
    op.execute(_bump("long_job_policy"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    mine = f"feature_values WHERE surface = 'migration' AND key IN {_KEYS}"
    op.execute(_bump(mine))
    op.execute(f"DELETE FROM {mine}")
