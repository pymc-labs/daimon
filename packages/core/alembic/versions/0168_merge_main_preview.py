"""Join main's migration head with the preview lane's head.

0158_usage_agent_backfill (main) and 0167_cost_footer (preview) both descend
from 0131_usage_obs_tenant_idx. Neither branch is changed, since either may
already be applied; this merge only joins them into one head.

downgrade: safe
"""

from __future__ import annotations

revision: str = "0168_merge_main_preview"
down_revision: tuple[str, str] = ("0158_usage_agent_backfill", "0167_cost_footer")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
