"""Remember which budget window a channel's admins were told is used up.

`channel_budgets.exhausted_notice_key` holds the window (`YYYY-MM` for a
monthly budget, `window` otherwise) whose exhausted notice was sent; setting
or raising the budget clears it. Claiming it in one UPDATE keeps the notice
to one per window across workers.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0043_channel_budget_notices"
down_revision: str | None = "0042_channel_budget_promo_codes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("channel_budgets", sa.Column("exhausted_notice_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("channel_budgets", "exhausted_notice_key")
