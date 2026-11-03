"""Add the per-tenant switch that hides the finished-turn cost footer.

On for every existing tenant, so every answer keeps its footer until an admin
hides it.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0167_cost_footer"
down_revision: str | None = "0166_watch_notice_refused"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "tenants",
        sa.Column("cost_footer", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("tenants", "cost_footer")
