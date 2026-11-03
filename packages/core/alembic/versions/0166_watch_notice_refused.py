"""Owner notice receipts can record a send the platform refused for good.

A `refused` receipt ends its notice: the owner closed their DMs, left the
server or is a bot, so the notice is not retried.

downgrade: destructive
"""

from alembic import op

revision = "0166_watch_notice_refused"
down_revision = "0165_agent_daimon_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_watch_notice_outcome", "watch_notice_receipts", type_="check")
    op.create_check_constraint(
        "ck_watch_notice_outcome",
        "watch_notice_receipts",
        "outcome IN ('pending', 'sent', 'failed', 'interrupted', 'refused')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("UPDATE watch_notice_receipts SET outcome='failed' WHERE outcome='refused'")
    op.drop_constraint("ck_watch_notice_outcome", "watch_notice_receipts", type_="check")
    op.create_check_constraint(
        "ck_watch_notice_outcome",
        "watch_notice_receipts",
        "outcome IN ('pending', 'sent', 'failed', 'interrupted')",
    )
