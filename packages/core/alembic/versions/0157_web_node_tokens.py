"""web_node_tokens: the web app keeps only tokens exchanged for connection nodes.

Web sign-in is Discord only, so the OpenID Connect leftovers go: `web_oidc_tokens`
becomes `web_node_tokens` keyed by `token_key` (`<issuer>#<server>`), dropping sign-in
token rows and refresh tokens; `web_group_roles` and the per-workspace sign-in switches
(`web_workspace_settings.signin_off`) are dropped. Downgrading restores the shapes empty.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0157_web_node_tokens"
down_revision: str | None = "0156_workspace_members"
branch_labels: str | None = None
depends_on: str | None = None

_RENAMES = (
    ("pk_web_oidc_tokens", "pk_web_node_tokens"),
    ("fk_web_oidc_tokens_tenants", "fk_web_node_tokens_tenants"),
    ("fk_web_oidc_tokens_people", "fk_web_node_tokens_people"),
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Exchanged tokens are keyed `<issuer>#<server>`; anything else came from a sign-in.
    op.execute("DELETE FROM web_oidc_tokens WHERE position('#' in issuer) = 0")
    op.rename_table("web_oidc_tokens", "web_node_tokens")
    op.alter_column("web_node_tokens", "issuer", new_column_name="token_key")
    op.drop_column("web_node_tokens", "refresh_token_enc")
    for old, new in _RENAMES:
        op.execute(f"ALTER TABLE web_node_tokens RENAME CONSTRAINT {old} TO {new}")
    op.drop_table("web_group_roles")
    op.drop_column("web_workspace_settings", "signin_off")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "web_workspace_settings",
        sa.Column("signin_off", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_table(
        "web_group_roles",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("group", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "issuer", "group", name="pk_web_group_roles"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_web_group_roles_tenants"
        ),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_web_group_roles"),
    )
    for old, new in _RENAMES:
        op.execute(f"ALTER TABLE web_node_tokens RENAME CONSTRAINT {new} TO {old}")
    op.add_column(
        "web_node_tokens", sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True)
    )
    op.alter_column("web_node_tokens", "token_key", new_column_name="issuer")
    op.rename_table("web_node_tokens", "web_oidc_tokens")
