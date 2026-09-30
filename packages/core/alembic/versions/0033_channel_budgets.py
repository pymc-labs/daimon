"""Per-channel spend budgets and the channel attribution they read.

`usage_events` and `tenant_ledger` gain a nullable `channel_id`: the parent
channel of the turn (or tool call) that spent the money. Only rows written
after this migration carry one, so existing rows count toward no channel.
`routines.channel_id` is the channel a routine's spend is attributed to,
backfilled from destinations whose channel is known without a platform call
(a channel, or a Slack thread's channel).

`channel_budgets` holds at most one budget per (tenant, platform, channel).
No row means no budget, so nothing is gated until an admin sets one.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0033_channel_budgets"
down_revision = "0032_promo_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("channel_id", sa.Text(), nullable=True))
    op.add_column("tenant_ledger", sa.Column("channel_id", sa.Text(), nullable=True))
    op.create_index(
        "tenant_ledger_tenant_channel_idx",
        "tenant_ledger",
        ["tenant_id", "channel_id", "occurred_at"],
        postgresql_where=sa.text("channel_id IS NOT NULL"),
    )
    op.add_column("routines", sa.Column("channel_id", sa.Text(), nullable=True))
    op.execute("UPDATE routines SET channel_id = destination_id WHERE destination_kind = 'channel'")
    op.execute(
        "UPDATE routines SET channel_id = split_part(routines.destination_id, ':', 1) "
        "FROM tenants WHERE tenants.id = routines.tenant_id "
        "AND tenants.platform = 'slack' AND routines.destination_kind = 'thread'"
    )
    op.create_table(
        "channel_budgets",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("limit_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "set_by_account_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "platform", "channel_id", name="uq_channel_budgets_tenant_channel"
        ),
        sa.CheckConstraint("limit_usd >= 0", name="ck_channel_budgets_limit"),
        sa.CheckConstraint(
            "\"window\" IN ('monthly', 'total', 'fixed')", name="ck_channel_budgets_window"
        ),
        sa.CheckConstraint(
            "(\"window\" = 'fixed' AND starts_at IS NOT NULL AND ends_at IS NOT NULL "
            "AND starts_at < ends_at) "
            "OR (\"window\" = 'total' AND ends_at IS NULL) "
            "OR (\"window\" = 'monthly' AND starts_at IS NULL AND ends_at IS NULL)",
            name="ck_channel_budgets_bounds",
        ),
    )


def downgrade() -> None:
    op.drop_table("channel_budgets")
    op.drop_column("routines", "channel_id")
    op.drop_index("tenant_ledger_tenant_channel_idx", table_name="tenant_ledger")
    op.drop_column("tenant_ledger", "channel_id")
    op.drop_column("usage_events", "channel_id")
