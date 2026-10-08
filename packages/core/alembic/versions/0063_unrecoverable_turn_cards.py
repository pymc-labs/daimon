"""Record aged Discord turn cards whose pending state cannot be recovered.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0063_unrecoverable_turn_cards"
down_revision: str | None = "0062_github_connect_origin"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint("ck_turn_card_intents_status", "turn_card_intents", type_="check")
    op.drop_constraint("ck_turn_card_intents_message_state", "turn_card_intents", type_="check")
    op.create_check_constraint(
        "ck_turn_card_intents_status",
        "turn_card_intents",
        "status IN ('prepared', 'posted', 'retired', 'unrecoverable')",
    )
    op.create_check_constraint(
        "ck_turn_card_intents_message_state",
        "turn_card_intents",
        "(status = 'prepared' AND message_id IS NULL) OR "
        "(status = 'posted' AND message_id IS NOT NULL AND message_id <> '') OR "
        "(status IN ('retired', 'unrecoverable') AND (message_id IS NULL OR message_id <> ''))",
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE turn_card_intents SET status = 'retired' WHERE status = 'unrecoverable'")
    )
    op.drop_constraint("ck_turn_card_intents_status", "turn_card_intents", type_="check")
    op.drop_constraint("ck_turn_card_intents_message_state", "turn_card_intents", type_="check")
    op.create_check_constraint(
        "ck_turn_card_intents_status",
        "turn_card_intents",
        "status IN ('prepared', 'posted', 'retired')",
    )
    op.create_check_constraint(
        "ck_turn_card_intents_message_state",
        "turn_card_intents",
        "(status = 'prepared' AND message_id IS NULL) OR "
        "(status = 'posted' AND message_id IS NOT NULL AND message_id <> '') OR "
        "(status = 'retired' AND (message_id IS NULL OR message_id <> ''))",
    )
