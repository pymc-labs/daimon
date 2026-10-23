"""Turn plain-text "@<bot name>" mentions on for every tenant created from now on.

Only the column default changes. An existing tenant keeps its current value:
nothing records whether an admin chose "off" or it was never touched, so no
row is flipped.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0125_plain_mentions_default_on"
down_revision: str | None = "0123_tenant_backends_explicit"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column("tenants", "plain_text_mentions", server_default=sa.text("true"))


def downgrade() -> None:
    op.alter_column("tenants", "plain_text_mentions", server_default=sa.text("false"))
