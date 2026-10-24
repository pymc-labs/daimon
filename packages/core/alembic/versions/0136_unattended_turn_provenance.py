"""Record which turn of a Managed Agents session agent chat sent.

`usage_reconcile_progress` gets the boundary (`user.message` id) of the
latest turn an agent-chat send started on the session, and the channel whose
tool rules that turn runs under. Only a pause on that turn may be resumed by
the owner's `continue_turn`; a pause on any other turn (a chat card) is left
to the flow that asked it.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0136_unattended_turn_provenance"
down_revision: str | None = "0135_routine_dm_destination"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "usage_reconcile_progress", sa.Column("unattended_turn_event_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "usage_reconcile_progress",
        sa.Column("unattended_rules_channel_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("usage_reconcile_progress", "unattended_rules_channel_id")
    op.drop_column("usage_reconcile_progress", "unattended_turn_event_id")
