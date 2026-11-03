"""tag_opt_outs: people who asked never to be tagged

Revision ID: 0179_tag_opt_outs
Revises: 0178_reach_tallies

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0179_tag_opt_outs"
down_revision: str | None = "0178_reach_tallies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tag_opt_outs",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
    )


def downgrade() -> None:
    op.drop_table("tag_opt_outs")
