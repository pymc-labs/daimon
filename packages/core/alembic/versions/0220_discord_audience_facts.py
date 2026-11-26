"""Discord room audience facts, and the role ids of each mirrored member.

`workspace_members.role_ids` keeps each member's role ids (NULL: not recorded), so a
person's standing is read from the current internal roles with no reload.
`discord_audience_state` says per server whether its room facts are complete and until
when they stay fresh. `discord_channel_audience` holds each room's contained and narrow
facts. Both tables are written only by the Discord gateway owner. Additive only.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0220_discord_audience_facts"
down_revision: str | None = "0219_channel_budget_alerts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "workspace_members",
        sa.Column("role_ids", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.create_table(
        "discord_audience_state",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("complete", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("fresh_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_discord_audience_state_tenants",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_discord_audience_state"),
    )
    op.create_table(
        "discord_channel_audience",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("contained", sa.Boolean(), nullable=False),
        sa.Column("narrow", sa.Boolean(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_discord_channel_audience_tenants",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_discord_channel_audience"),
    )


def downgrade() -> None:
    op.drop_table("discord_channel_audience")
    op.drop_table("discord_audience_state")
    op.drop_column("workspace_members", "role_ids")
