"""Graph change-notification subscriptions to Teams private and shared channels.

`teams_channel_subscriptions` holds, per channel, the subscription id, when it
expires, a hash of its client-state secret and the newest message handled. It
starts empty: channels are subscribed as the adapter sees them.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0103_teams_channel_subscriptions"
down_revision: str | None = "0102_agent_backend_drop_consent"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "teams_channel_subscriptions",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("channel_name", sa.Text(), nullable=True),
        sa.Column("team_name", sa.Text(), nullable=True),
        sa.Column("bot_name", sa.Text(), nullable=True),
        sa.Column("service_url", sa.Text(), nullable=True),
        sa.Column("subscription_id", sa.Text(), nullable=False),
        sa.Column("client_state_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_id", sa.Text(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_teams_channel_subscriptions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_teams_channel_subscriptions_tenants",
        ),
        sa.UniqueConstraint(
            "subscription_id", name="uq_teams_channel_subscriptions_subscription_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("teams_channel_subscriptions")
