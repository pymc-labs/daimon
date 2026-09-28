"""Remember which tenant a person's direct messages go to.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_dm_tenant_selections"
down_revision: str | None = "0027_turn_card_intents"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "dm_tenant_selections",
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_user_id", sa.Text(), nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "selected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("platform", "external_user_id"),
    )
    op.create_index("ix_dm_tenant_selections_tenant_id", "dm_tenant_selections", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_dm_tenant_selections_tenant_id", table_name="dm_tenant_selections")
    op.drop_table("dm_tenant_selections")
