"""Mark a running turn whose caller may be from another organisation.

`turn_origins.is_external` is true while a turn runs for a caller the adapter
could not place, or placed in another organisation. Such a turn is answered
as an external participant's, so its MCP calls are too, though nothing about
the caller was stored. Rows live only as long as their turn.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0047_turn_origin_external"
down_revision: str | None = "0046_account_external"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "turn_origins",
        sa.Column("is_external", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("turn_origins", "is_external")
