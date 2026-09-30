"""Allow a per-tenant concurrent-turn cap override.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_tenant_turn_cap"
down_revision = "0030_feat085_routine_destination"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("turn_cap", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_tenants_turn_cap", "tenants", "turn_cap IS NULL OR turn_cap > 0")


def downgrade() -> None:
    op.drop_constraint("ck_tenants_turn_cap", "tenants", type_="check")
    op.drop_column("tenants", "turn_cap")
