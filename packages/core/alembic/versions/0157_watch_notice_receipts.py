"""Delivery receipts for watch owner notices.

downgrade: destructive
"""

from alembic import op

revision = "0157_watch_notice_receipts"
down_revision = "0156_watch_mcp_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # No foreign key to watches: a receipt outlives its watch until pruned.
    op.execute("""CREATE TABLE watch_notice_receipts (
        id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        watch_id UUID NOT NULL,
        owner_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        kind TEXT NOT NULL,
        generation INTEGER NOT NULL,
        attempt INTEGER NOT NULL,
        claim TEXT NOT NULL,
        outcome TEXT DEFAULT 'pending' NOT NULL,
        attempted_at TIMESTAMP WITH TIME ZONE NOT NULL,
        settled_at TIMESTAMP WITH TIME ZONE,
        channel_id TEXT,
        message_id TEXT,
        CONSTRAINT uq_watch_notice_attempt UNIQUE (watch_id, kind, generation, attempt),
        CONSTRAINT ck_watch_notice_outcome CHECK
            (outcome IN ('pending', 'sent', 'failed', 'interrupted'))
    )""")
    op.execute("CREATE INDEX ix_watch_notice_receipt_time ON watch_notice_receipts (attempted_at)")


def downgrade() -> None:
    op.execute("DROP TABLE watch_notice_receipts")
