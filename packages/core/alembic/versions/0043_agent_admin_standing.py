"""Record what makes an agent a channel admin's to administer.

`agent_creation_channels` names the channel an agent was created for, when a
channel admin of that channel created it from there. `channel_config.
agent_name_set_by_admin` says a server admin set the channel's default, as
decided when it was set. Existing defaults are backfilled from the setter's
stored role; existing agents have no creation channel.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_agent_admin_standing"
down_revision: str | None = "0042_channel_skills"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agent_creation_channels",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ma_agent_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "ma_agent_id", name="pk_agent_creation_channels"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_agent_creation_channels_tenants",
        ),
        sa.CheckConstraint(
            "platform IN ('discord', 'slack', 'teams')", name="ck_agent_creation_channels_platform"
        ),
    )
    op.add_column(
        "channel_config",
        sa.Column(
            "agent_name_set_by_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.execute(
        "UPDATE channel_config SET agent_name_set_by_admin = true FROM accounts "
        "WHERE accounts.id = channel_config.agent_name_set_by_account_id "
        "AND accounts.role = 'admin' AND channel_config.agent_name IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("channel_config", "agent_name_set_by_admin")
    op.drop_table("agent_creation_channels")
