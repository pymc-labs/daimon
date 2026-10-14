"""Join agent backend settings with the main usage reconcile merge.

0091_agent_backend_settings and 0090_merge_main_usage_reconcile both follow
0089_neutral_catalog. Neither is changed; this revision only joins the two
branches into one head.

downgrade: safe
"""

from __future__ import annotations

revision: str = "0092_merge_agent_backend"
down_revision: tuple[str, str] = ("0090_merge_main_usage_reconcile", "0091_agent_backend_settings")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
