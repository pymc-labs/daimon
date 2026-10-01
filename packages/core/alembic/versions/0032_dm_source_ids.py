"""Record the channel and thread a DM conversation was moved from.

Lets each private turn re-check the source against the current seal list:
the channel and thread /dm ran in, and (Slack) the ``channel:thread_ts`` key
of every copied message. Rows written before this revision keep NULLs, and
are quarantined on their next turn once the tenant seals anything. Deploy this
migration before the code that reads the columns.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_dm_source_ids"
down_revision = "0031_tenant_turn_cap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "direct_message_conversations", sa.Column("source_channel_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "direct_message_conversations", sa.Column("source_thread_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "direct_message_conversations",
        sa.Column("source_thread_keys", sa.ARRAY(sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("direct_message_conversations", "source_thread_keys")
    op.drop_column("direct_message_conversations", "source_thread_id")
    op.drop_column("direct_message_conversations", "source_channel_id")
