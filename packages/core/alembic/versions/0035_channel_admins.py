"""Channel admins, and the platform role ids an account held on its last turn.

`channel_admins` names, per channel, the roles and users who administer it on
top of the server or workspace admins. No row means nobody extra, so existing
tenants behave exactly as before. `accounts.platform_role_ids` is refreshed on
every chat turn like `accounts.role`, so MCP calls can match a role grant
without a live platform lookup; it starts empty.
`task_continuations_waiting_idx` serves agent reach's read of the wakes still
owed to an agent. `thread_sessions.channel_id` records the channel a session
runs for when it is created, so agent reach places it without waiting for its
spend; older live rows take their latest spend's channel (a Slack thread id is
the bare thread ts, so it never names one). Agent reach reads live rows only,
so the backfill touches no other.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035_channel_admins"
down_revision: str | None = "0034_channel_budgets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "platform_role_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.create_table(
        "channel_admins",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column(
            "role_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "user_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("updated_by_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "platform", "channel_id", name="pk_channel_admins"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_channel_admins_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_channel_admins_updated_by_account_id",
        ),
        sa.CheckConstraint("platform IN ('discord', 'slack')", name="ck_channel_admins_platform"),
    )
    op.create_index(
        "task_continuations_waiting_idx",
        "task_continuations",
        ["tenant_id", "target_name"],
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )
    op.add_column("thread_sessions", sa.Column("channel_id", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE thread_sessions AS ts SET channel_id = latest.channel_id
        FROM (
            SELECT DISTINCT ON (live.id) live.id, spend.channel_id
            FROM thread_sessions AS live
            JOIN usage_events AS spend
                ON spend.managed_session_id = live.ma_session_id
                AND spend.tenant_id = live.tenant_id
            WHERE live.status = 'live' AND spend.channel_id IS NOT NULL
            ORDER BY live.id, spend.occurred_at DESC
        ) AS latest
        WHERE ts.id = latest.id
        """
    )


def downgrade() -> None:
    op.drop_column("thread_sessions", "channel_id")
    op.drop_index("task_continuations_waiting_idx", table_name="task_continuations")
    op.drop_table("channel_admins")
    op.drop_column("accounts", "platform_role_ids")
