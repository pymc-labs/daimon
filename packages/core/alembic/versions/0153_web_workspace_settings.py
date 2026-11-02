"""web_workspace_settings: what a workspace's admins switched off in the web app.

One optional row per workspace: the modules and sign-in methods turned off, and the
agent a new chat starts with. No row means everything the deployment offers is on.
Additive only; downgrading drops these choices.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0153_web_workspace_settings"
down_revision: str | None = "0152_web_invites"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "web_workspace_settings",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("modules_off", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("signin_off", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("default_agent", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_web_workspace_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_web_workspace_settings_tenants",
        ),
    )


def downgrade() -> None:
    op.drop_table("web_workspace_settings")
