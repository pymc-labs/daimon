"""workspace_members: who belongs to each chat workspace, as its platform says.

The chat adapters load each workspace's member list and keep it current from join,
leave and role events; `workspace_member_syncs` marks the workspaces loaded in full.
The web app reads both to keep a person's access in step with their chat membership.
Also indexes `platform_principals.account_id`, which every web request reads. That
index is built concurrently, outside the migration transaction and before the tables,
so principals keep being written meanwhile and a run cut off mid-build is simply
retried: a leftover invalid index is dropped and built again.
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

PRINCIPAL_ACCOUNT_INDEX = "ix_platform_principals_account_id"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": PRINCIPAL_ACCOUNT_INDEX},
        )
        if invalid:
            op.drop_index(
                PRINCIPAL_ACCOUNT_INDEX,
                table_name="platform_principals",
                postgresql_concurrently=True,
            )
        op.create_index(
            PRINCIPAL_ACCOUNT_INDEX,
            "platform_principals",
            ["account_id"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
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


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("workspace_member_syncs")
    op.drop_table("workspace_members")
    with op.get_context().autocommit_block():
        op.drop_index(
            PRINCIPAL_ACCOUNT_INDEX,
            table_name="platform_principals",
            if_exists=True,
            postgresql_concurrently=True,
        )
