"""Count unresolved Discord card recovery passes across process restarts.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0065_turn_card_recovery_failures"
down_revision: str | None = "0064_unrecoverable_turn_cards"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "turn_card_intents",
        sa.Column("recovery_failures", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("turn_card_intents", "recovery_failures")
