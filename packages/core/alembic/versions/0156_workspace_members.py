"""workspace_members: who belongs to each chat workspace, as its platform says.

The chat adapters load each workspace's member list and keep it current from join,
leave and role events; `workspace_member_syncs` marks the workspaces loaded in full.
The web app reads both to keep a person's access in step with their chat membership.
Also indexes `platform_principals.account_id`, which every web request reads.
Downgrading drops the lists; the next start loads them again.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0156_workspace_members"
down_revision: str | None = "0155_web_oidc_tokens"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "workspace_members",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform_user_id", sa.Text(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "platform_user_id", name="pk_workspace_members"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_workspace_members_tenants"
        ),
    )
    op.create_table(
        "workspace_member_syncs",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_workspace_member_syncs"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_workspace_member_syncs_tenants",
        ),
    )
    op.create_index("ix_platform_principals_account_id", "platform_principals", ["account_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_platform_principals_account_id", table_name="platform_principals")
    op.drop_table("workspace_member_syncs")
    op.drop_table("workspace_members")
