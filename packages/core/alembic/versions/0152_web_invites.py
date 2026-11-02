"""web_invites: invite links, one-time sign-in links, and who may chat with an agent.

Adds `web_invites` (only the SHA-256 of a link's token is stored), `web_invite_agents`
(the agents an invite opens) and `web_agent_grants` (an agent's web chat switch and
who it serves; no row means on, for everyone). An accepted invite is deleted with the
person who accepted it, so erasing them removes the email it was sent to. Additive
only; downgrading drops every invite and grant.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0152_web_invites"
down_revision: str | None = "0151_agent_drafts"
branch_labels: str | None = None
depends_on: str | None = None


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name=f"fk_{table}_tenants"
    )


def _now(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "web_invites",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False, server_default=""),
        sa.Column("role", sa.Text(), nullable=False, server_default="visitor"),
        sa.Column("credit_usd", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("person_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_account_id", UUID(as_uuid=True), nullable=True),
        _now("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_person_id", UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_web_invites"),
        _tenant_fk("web_invites"),
        sa.ForeignKeyConstraint(
            ["person_id"], ["web_people.id"], ondelete="CASCADE", name="fk_web_invites_person"
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by_person_id"],
            ["web_people.id"],
            ondelete="CASCADE",
            name="fk_web_invites_accepted_by",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_web_invites_created_by",
        ),
        sa.UniqueConstraint("token_sha256", name="uq_web_invites_token_sha256"),
        sa.CheckConstraint("kind IN ('invite', 'sign_in', 'owner')", name="ck_web_invites_kind"),
        sa.CheckConstraint("role IN ('visitor', 'member')", name="ck_web_invites_role"),
    )
    op.create_index("ix_web_invites_tenant_created", "web_invites", ["tenant_id", "created_at"])
    op.create_table(
        "web_invite_agents",
        sa.Column("invite_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("invite_id", "agent_id", name="pk_web_invite_agents"),
        _tenant_fk("web_invite_agents"),
        sa.ForeignKeyConstraint(
            ["invite_id"],
            ["web_invites.id"],
            ondelete="CASCADE",
            name="fk_web_invite_agents_invite",
        ),
    )
    op.create_table(
        "web_agent_grants",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("web", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("who", sa.Text(), nullable=False, server_default="everyone"),
        sa.Column("groups", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_web_agent_grants"),
        _tenant_fk("web_agent_grants"),
        sa.CheckConstraint(
            "who IN ('everyone', 'groups', 'invites')", name="ck_web_agent_grants_who"
        ),
    )


def downgrade() -> None:
    op.drop_table("web_agent_grants")
    op.drop_table("web_invite_agents")
    op.drop_index("ix_web_invites_tenant_created", table_name="web_invites")
    op.drop_table("web_invites")
