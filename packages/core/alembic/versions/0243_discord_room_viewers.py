"""Who can view each Discord room, for data reads.

`discord_room_viewers` holds each room's viewer user ids under the room facts'
generation. Written by the Discord gateway owner only while a workspace has data
sources on. Additive only.

downgrade: destructive
schema: expand
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0243_discord_room_viewers"
down_revision: str | None = "0242_data_search"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "discord_room_viewers",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("user_ids", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_discord_room_viewers_tenants",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_discord_room_viewers"),
    )


def downgrade() -> None:
    op.drop_table("discord_room_viewers")
