"""Add optional content-free usage metrics to outcomes; historical rows stay unknown.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030_sys066_turn_usage"
down_revision = "0029_feat084_timers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        op.add_column("turn_outcomes", sa.Column(name, sa.BigInteger(), nullable=True))
    for name in ("model_calls", "unpriced_calls"):
        op.add_column("turn_outcomes", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column("turn_outcomes", sa.Column("model_ids", postgresql.JSONB(), nullable=True))
    op.add_column("turn_outcomes", sa.Column("cost_usd", sa.Numeric(20, 10), nullable=True))
    op.add_column("turn_outcomes", sa.Column("billing_posture", sa.Text(), nullable=True))


def downgrade() -> None:
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "model_calls",
        "unpriced_calls",
        "model_ids",
        "cost_usd",
        "billing_posture",
    ):
        op.drop_column("turn_outcomes", name)
