"""Teams installs, and Teams channel admins.

`teams_installations` records each team the bot is in and its Entra group id,
so the MCP server can list and read Teams channels. It starts empty and fills
from each team's next activity or install event. `channel_admins` accepts
`teams` rows.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_teams_parity"
down_revision: str | None = "0037_mcp_token_channels"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "teams_installations",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "installed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "team_id", name="pk_teams_installations"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_teams_installations_tenants"
        ),
    )
    op.drop_constraint("ck_channel_admins_platform", "channel_admins", type_="check")
    op.create_check_constraint(
        "ck_channel_admins_platform", "channel_admins", "platform IN ('discord', 'slack', 'teams')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM channel_admins WHERE platform = 'teams'")
    op.drop_constraint("ck_channel_admins_platform", "channel_admins", type_="check")
    op.create_check_constraint(
        "ck_channel_admins_platform", "channel_admins", "platform IN ('discord', 'slack')"
    )
    op.drop_table("teams_installations")
