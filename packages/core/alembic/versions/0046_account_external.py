"""Mark accounts that belong to another organisation.

`accounts.is_external` is true for a person from another organisation, such
as an external participant in a Teams shared channel. Admission writes it
only on positive evidence of the person's organisation. Such an account is
never an admin, and MCP tools refuse it all but the conversation's.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0046_account_external"
down_revision: str | None = "0045_admission_refusal_reasons"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("is_external", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("accounts", "is_external")
