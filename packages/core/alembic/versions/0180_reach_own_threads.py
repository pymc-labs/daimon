"""reach_tallies: untagged threads opened in the own channel

Revision ID: 0180_reach_own_threads
Revises: 0179_tag_opt_outs

downgrade: safe
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0180_reach_own_threads"
down_revision: str | None = "0179_tag_opt_outs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reach_tallies",
        sa.Column("own_threads", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("reach_tallies", "own_threads")
