"""Join main's usage reconcile progress with the neutral-core revisions.

Main added 0087_usage_reconcile_progress and neutral core added
0087_neutral_state -> 0088 -> 0089_neutral_catalog, both after
0086_discord_gateway_lease. Neither is renumbered: main's revision may
already be applied. This revision only joins the two branches.

downgrade: safe
"""

from __future__ import annotations

revision: str = "0090_merge_main_usage_reconcile"
down_revision: tuple[str, str] = ("0089_neutral_catalog", "0087_usage_reconcile_progress")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
