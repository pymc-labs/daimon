"""web_people: who the web app knows, how they sign in and their browser sessions.

Adds `web_people` (one row per person per workspace), `web_identities`,
`web_sessions` and `web_session_people`, `web_oidc_tokens`, `workspace_admins`,
`web_group_roles` and `web_audit`. Person-scoped rows cascade from the account, so an
account purge removes them. Additive only; downgrading drops every web sign-in and role.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0148_web_people"
down_revision: str | None = "0147_agent_notices"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = (
    "web_audit",
    "web_group_roles",
    "workspace_admins",
    "web_oidc_tokens",
    "web_session_people",
    "web_sessions",
    "web_identities",
    "web_people",
)


def _id() -> sa.Column[object]:
    return sa.Column(
        "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
    )


def _now(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name=f"fk_{table}_tenants"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "web_people",
        _id(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("workspace_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("invite_id", UUID(as_uuid=True), nullable=True),
        sa.Column("invited_by_account_id", UUID(as_uuid=True), nullable=True),
        _now("last_seen_at"),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_web_people"),
        _tenant_fk("web_people"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE", name="fk_web_people_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_web_people_invited_by",
        ),
        sa.UniqueConstraint("tenant_id", "account_id", name="uq_web_people_tenant_account"),
        sa.CheckConstraint("kind IN ('member', 'visitor')", name="ck_web_people_kind"),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_web_people_status"),
    )
    op.create_table(
        "web_identities",
        _id(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False, server_default=""),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("groups", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        _now("linked_at"),
        sa.PrimaryKeyConstraint("id", name="pk_web_identities"),
        _tenant_fk("web_identities"),
        sa.ForeignKeyConstraint(
            ["person_id"], ["web_people.id"], ondelete="CASCADE", name="fk_web_identities_people"
        ),
        sa.UniqueConstraint(
            "tenant_id", "provider", "issuer", "subject", name="uq_web_identities_subject"
        ),
    )
    op.create_index("ix_web_identities_lookup", "web_identities", ["provider", "issuer", "subject"])
    op.create_index("ix_web_identities_person", "web_identities", ["person_id"])
    op.create_table(
        "web_sessions",
        _id(),
        sa.Column("token_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False, server_default=""),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("current_person_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        _now("created_at"),
        _now("last_seen_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _now("roles_checked_at"),
        sa.PrimaryKeyConstraint("id", name="pk_web_sessions"),
        _tenant_fk("web_sessions"),
        sa.ForeignKeyConstraint(
            ["current_person_id"],
            ["web_people.id"],
            ondelete="CASCADE",
            name="fk_web_sessions_people",
        ),
        sa.UniqueConstraint("token_sha256", name="uq_web_sessions_token_sha256"),
    )
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])
    op.create_table(
        "web_session_people",
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "person_id", name="pk_web_session_people"),
        _tenant_fk("web_session_people"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["web_sessions.id"],
            ondelete="CASCADE",
            name="fk_web_session_people_sessions",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["web_people.id"],
            ondelete="CASCADE",
            name="fk_web_session_people_people",
        ),
    )
    op.create_table(
        "web_oidc_tokens",
        sa.Column("person_id", UUID(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("person_id", "issuer", name="pk_web_oidc_tokens"),
        _tenant_fk("web_oidc_tokens"),
        sa.ForeignKeyConstraint(
            ["person_id"], ["web_people.id"], ondelete="CASCADE", name="fk_web_oidc_tokens_people"
        ),
    )
    op.create_table(
        "workspace_admins",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("granted_by_account_id", UUID(as_uuid=True), nullable=True),
        _now("granted_at"),
        sa.PrimaryKeyConstraint("tenant_id", "account_id", name="pk_workspace_admins"),
        _tenant_fk("workspace_admins"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE", name="fk_workspace_admins_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_workspace_admins_granted_by",
        ),
        sa.CheckConstraint("role IN ('owner', 'admin')", name="ck_workspace_admins_role"),
    )
    op.create_table(
        "web_group_roles",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("group", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "issuer", "group", name="pk_web_group_roles"),
        _tenant_fk("web_group_roles"),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_web_group_roles"),
    )
    op.create_table(
        "web_audit",
        _id(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        _now("at"),
        sa.Column("actor_account_id", UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_web_audit"),
        _tenant_fk("web_audit"),
        sa.ForeignKeyConstraint(
            ["actor_account_id"], ["accounts.id"], ondelete="SET NULL", name="fk_web_audit_actor"
        ),
    )
    op.create_index("ix_web_audit_tenant_at", "web_audit", ["tenant_id", "at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in _TABLES:
        op.drop_table(table)
