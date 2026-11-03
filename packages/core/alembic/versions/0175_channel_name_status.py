"""`platform_channel_names.status`: whether the platform still has the channel.

A channel with settings can be deleted on its platform, or be a direct message that was
configured by mistake. The listing round asks the platform about such ids and stores the
answer here: `named` (the default), `gone` or `direct`. A gone or direct row may hold an
empty name. Downgrading drops the column and the answers.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0175_channel_name_status"
down_revision: str | None = "0174_drop_channel_admins"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "platform_channel_names",
        sa.Column("status", sa.Text(), nullable=False, server_default="named"),
    )
    op.create_check_constraint(
        "ck_platform_channel_names_status",
        "platform_channel_names",
        "status IN ('named', 'gone', 'direct')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_platform_channel_names_status", "platform_channel_names", type_="check")
    op.drop_column("platform_channel_names", "status")
