"""agent_posted_messages: the role of a turn or routine post

Revision ID: 0185_agent_post_role
Revises: 0184_coding_key_claims

downgrade: safe
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0185_agent_post_role"
down_revision: str | None = "0184_coding_key_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_posted_messages", sa.Column("role", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_agent_posted_messages_role",
        "agent_posted_messages",
        "role IN ('status', 'answer', 'notice', 'controls', 'routine')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_posted_messages_role", "agent_posted_messages", type_="check")
    op.drop_column("agent_posted_messages", "role")
