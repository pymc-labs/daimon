"""Keep calling human provenance separate from attended tool controls.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision = "0150_origin_human_invocation"
down_revision = "0149_context_provider_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("turn_origins", sa.Column("human_invocation", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("turn_origins", "human_invocation")
