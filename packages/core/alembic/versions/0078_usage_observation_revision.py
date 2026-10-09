"""Track the latest neutral revision projected into legacy usage rows.

Nullable, without a default or backfill, so existing writers and rows retain
their current behavior. The legacy golden column projection excludes it.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0078_usage_observation_revision"
down_revision: str | None = "0077_neutral_state"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("observation_revision", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_events", "observation_revision")
