"""reach_tallies: per-turn reach counts and the approved plan

Revision ID: 0178_reach_tallies
Revises: 0177_invite_files

downgrade: safe
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0178_reach_tallies"
down_revision: str | None = "0177_invite_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reach_tallies",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("turn_key", sa.Text(), nullable=False),
        sa.Column("threads", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "places", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "people", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("approved_threads", sa.Integer(), nullable=True),
        sa.Column("approved_places", sa.Integer(), nullable=True),
        sa.Column("approved_people", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "turn_key"),
    )
    op.create_index("ix_reach_tallies_updated_at", "reach_tallies", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_reach_tallies_updated_at", table_name="reach_tallies")
    op.drop_table("reach_tallies")
