"""web_search_usage: web searches on usage rows and the providers' free allowances.

`usage_events` gains `web_searches` (the most any revision reported),
`free_web_searches` (those the free allowance covered) and `paid_web_searches`
(the latest revision's billed ones), so spend reads price them.
`web_search_allowances` counts searches per provider account, allowance pool and
calendar month, shared by every tenant on that account. Downgrading drops the
columns and the table, so spend reads stop counting searches already debited.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0205_web_search_usage"
down_revision: str | None = "0204_shared_agent_copies"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for column in ("web_searches", "free_web_searches", "paid_web_searches"):
        op.add_column(
            "usage_events",
            sa.Column(column, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    op.create_table(
        "web_search_allowances",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account_scope_id", sa.Text(), nullable=False),
        sa.Column("pool", sa.Text(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "provider", "account_scope_id", "pool", "period", name="pk_web_search_allowances"
        ),
        sa.CheckConstraint("used >= 0", name="ck_web_search_allowances_used"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("web_search_allowances")
    for column in ("paid_web_searches", "free_web_searches", "web_searches"):
        op.drop_column("usage_events", column)
