"""Record who is from another organisation and which channels are shared.

`platform_principals.foreign_home_id` holds the home organisation id of a person
verified to belong to another organisation: written once, never cleared by code.
`turn_origins.shared_channel` marks a turn whose origin channel is shared with
another organisation. `shared_channels` remembers each such channel, with whether
the platform or a client's own message revealed it. Additive only.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0116_shared_channel_facts"
down_revision: str | None = "0115_progress_plan_scopes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("platform_principals", sa.Column("foreign_home_id", sa.Text(), nullable=True))
    op.add_column(
        "turn_origins",
        sa.Column("shared_channel", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "shared_channels",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint("source IN ('platform', 'client')", name="ck_shared_channels_source"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_shared_channels_tenants"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "platform", "channel_id", name="pk_shared_channels"),
    )


def downgrade() -> None:
    op.drop_table("shared_channels")
    op.drop_column("turn_origins", "shared_channel")
    op.drop_column("platform_principals", "foreign_home_id")
