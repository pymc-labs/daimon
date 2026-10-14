"""Join main's migration head with the neutral migration head.

0098_external_github_session (main) and 0096_agent_fleet_grant (neutral)
both descend from 0087_usage_reconcile_progress. Neither is changed; this
merge only joins the two branches into one head.

downgrade: safe
"""

from __future__ import annotations

revision: str = "0099_merge_main_neutral"
down_revision: tuple[str, str] = ("0096_agent_fleet_grant", "0098_external_github_session")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
