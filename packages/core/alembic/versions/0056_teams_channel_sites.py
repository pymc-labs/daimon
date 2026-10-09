"""Teams channels' Files folders on SharePoint sites granted to daimon.

`teams_channel_sites` records, per channel, the site, library and folder an
admin's sign-in found when they turned files on there. A private or shared
channel keeps its files on a site of its own, which daimon's `Sites.Selected`
permission cannot look up. It starts empty.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056_teams_channel_sites"
down_revision: str | None = "0055_github_token_audit_versions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "teams_channel_sites",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("site_id", sa.Text(), nullable=False),
        sa.Column("drive_id", sa.Text(), nullable=False),
        sa.Column("folder_id", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_teams_channel_sites"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_teams_channel_sites_tenants"
        ),
    )


def downgrade() -> None:
    op.drop_table("teams_channel_sites")
