"""Channel admins, the platform role ids an account held on its last turn, and DM origins.

`channel_admins` names, per channel, the roles and users who administer it on
top of the server or workspace admins. No row means nobody extra, so existing
tenants behave exactly as before. `accounts.platform_role_ids` is refreshed on
every chat turn like `accounts.role`, so MCP calls can match a role grant
without a live platform lookup; it starts empty.
`direct_message_conversations.source_channel_id` records the channel `/dm`
ran in, so a private conversation counts as that channel; existing rows keep
NULL and count as their DM channel until the member runs `/dm` again.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_channel_admins"
down_revision: str | None = "0033_channel_budgets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "direct_message_conversations", sa.Column("source_channel_id", sa.Text(), nullable=True)
    )
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


def downgrade() -> None:
    op.drop_table("channel_admins")
    op.drop_column("accounts", "platform_role_ids")
    op.drop_column("direct_message_conversations", "source_channel_id")
