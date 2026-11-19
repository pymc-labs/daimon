"""A support menu may name the channel its requests go to (`support_menus.channel`).

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0213_support_menu_channel"
down_revision: str | None = "0212_sandbox_tools_feature"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("support_menus", sa.Column("channel", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("support_menus", "channel")
