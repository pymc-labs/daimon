"""Track bot-managed Discord roles for exact agent selection.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049_discord_agent_roles"
down_revision: str | None = "0048_github_access_foundation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "discord_agent_roles",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ma_agent_id", sa.Text(), nullable=False),
        sa.Column("role_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "ma_agent_id", name="pk_discord_agent_roles"),
        sa.UniqueConstraint("tenant_id", "role_id", name="uq_discord_agent_roles_role"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("discord_agent_roles")
