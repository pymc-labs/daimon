"""Keep MA session deletion targets after account erasure.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0077_privacy_session_deletes"
down_revision: str | None = "0076_github_connect_followup"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "privacy_session_deletes",
        sa.Column("account_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_ids", JSONB(), nullable=False),
        sa.Column("pending_session_ids", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("privacy_session_deletes")
