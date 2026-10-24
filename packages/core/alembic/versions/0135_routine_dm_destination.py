"""Owner-only routine direct-message destinations.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0135_routine_dm_destination"
down_revision: str | None = "0134_cold_dm_agent"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "routines",
        sa.Column(
            "source_provenance_known", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("routines", sa.Column("source_channel_id", sa.Text(), nullable=True))
    op.add_column("routines", sa.Column("source_thread_id", sa.Text(), nullable=True))
    op.drop_constraint("ck_routines_destination_kind", "routines", type_="check")
    op.create_check_constraint(
        "ck_routines_destination_kind",
        "routines",
        "destination_kind IS NULL OR destination_kind IN ('channel', 'thread', 'dm')",
    )
    op.create_check_constraint(
        "ck_routines_dm_owner",
        "routines",
        "destination_kind IS DISTINCT FROM 'dm' OR "
        "(created_by_user_id IS NOT NULL AND destination_id = created_by_user_id)",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM routines WHERE destination_kind = 'dm')")
    ):
        raise RuntimeError("convert or remove DM routines before downgrade")
    op.drop_column("routines", "source_thread_id")
    op.drop_column("routines", "source_channel_id")
    op.drop_column("routines", "source_provenance_known")
    op.drop_constraint("ck_routines_dm_owner", "routines", type_="check")
    op.drop_constraint("ck_routines_destination_kind", "routines", type_="check")
    op.create_check_constraint(
        "ck_routines_destination_kind",
        "routines",
        "destination_kind IS NULL OR destination_kind IN ('channel', 'thread')",
    )
