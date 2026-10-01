"""Add explicit tenant funding policy with a prepaid default.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_tenant_funding_mode"
down_revision = "0027_turn_card_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants", sa.Column("funding_mode", sa.Text(), nullable=False, server_default="prepaid")
    )
    op.create_check_constraint(
        "ck_tenants_funding_mode", "tenants", "funding_mode IN ('prepaid', 'operator_funded')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenants_funding_mode", "tenants", type_="check")
    op.drop_column("tenants", "funding_mode")
