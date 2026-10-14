"""Add the per-tenant switch that lets a plain-text "@<bot name>" start a turn.

Off for every existing tenant, so nothing changes until an admin turns it on.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0100_plain_text_mentions"
down_revision: str | None = "0099_merge_main_neutral"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "plain_text_mentions", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "plain_text_mentions")
