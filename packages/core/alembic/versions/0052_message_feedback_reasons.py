"""Record the reasons picked in the "What went wrong?" feedback form.

`message_feedback.feedback_reasons` holds the reason codes a person ticked
(`daimon.core.message_feedback.FEEDBACK_REASONS`) next to the free text. NULL
means none were picked, which every row written before this revision reads as.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052_message_feedback_reasons"
down_revision: str | None = "0051_turn_origin_archive"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "message_feedback",
        sa.Column("feedback_reasons", postgresql.ARRAY(sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("message_feedback", "feedback_reasons")
