"""Channel budgets may alert instead of stopping.

`on_limit` is `stop` (a channel at its limit takes no new turns) or `alert`
(turns continue; Daimon posts to `alert_channel_id`, else the workspace's
support channel, at the limit and at each further full multiple of it).
`alert_window_key` and `alert_level` record the highest multiple already
alerted in a window, so each alert goes out once across workers and restarts.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0214_channel_budget_alerts"
down_revision: str | None = "0213_support_menu_channel"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "channel_budgets",
        sa.Column("on_limit", sa.Text(), nullable=False, server_default="stop"),
    )
    op.add_column("channel_budgets", sa.Column("alert_channel_id", sa.Text(), nullable=True))
    op.add_column("channel_budgets", sa.Column("alert_window_key", sa.Text(), nullable=True))
    op.add_column(
        "channel_budgets",
        sa.Column("alert_level", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_channel_budgets_on_limit", "channel_budgets", "on_limit IN ('stop', 'alert')"
    )
    op.create_check_constraint(
        "ck_channel_budgets_alert_channel",
        "channel_budgets",
        "alert_channel_id IS NULL OR on_limit = 'alert'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_channel_budgets_alert_channel", "channel_budgets", type_="check")
    op.drop_constraint("ck_channel_budgets_on_limit", "channel_budgets", type_="check")
    op.drop_column("channel_budgets", "alert_level")
    op.drop_column("channel_budgets", "alert_window_key")
    op.drop_column("channel_budgets", "alert_channel_id")
    op.drop_column("channel_budgets", "on_limit")
