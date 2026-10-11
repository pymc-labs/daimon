"""Record which Discord process holds the gateway lease.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0086_discord_gateway_lease"
down_revision: str | None = "0085_github_requested_repo"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "discord_gateway_lease",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("holder_owner_key", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
    )
    # Code from before the lease never sets this, so it marks owners that can
    # connect to the gateway without holding the lease.
    op.add_column(
        "discord_worker_heartbeats",
        sa.Column("lease_aware", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("discord_worker_heartbeats", "lease_aware")
    op.drop_table("discord_gateway_lease")
