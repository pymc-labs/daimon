"""Mirrors: copy an agent's agent-chat questions and answers into a channel.

`mirrors` holds each standing copy and its recorded consent;
`mirror_deliveries` is the outbox the destination platform's chat adapter
drains, one row per (mirror, event). Both are new; nothing existing changes.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0093_mirrors"
down_revision: str | None = "0092_routine_post_source"
branch_labels: str | None = None
depends_on: str | None = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "mirrors",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_agent_id", _UUID, nullable=False),
        sa.Column("source_agent_name", sa.Text(), nullable=False),
        sa.Column("source_agent_names", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("format", sa.Text(), nullable=False, server_default=sa.text("'qa'")),
        sa.Column(
            "dest_tenant_id",
            _UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dest_platform", sa.Text(), nullable=False),
        sa.Column("dest_kind", sa.Text(), nullable=False),
        sa.Column("dest_id", sa.Text(), nullable=False),
        sa.Column("consent_basis", sa.Text()),
        sa.Column("consent_note", sa.Text()),
        sa.Column(
            "consent_by_account_id", _UUID, sa.ForeignKey("accounts.id", ondelete="SET NULL")
        ),
        sa.Column("consent_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("paused_reason", sa.Text()),
        sa.Column("max_answer_chars", sa.Integer()),
        sa.Column(
            "created_by_account_id", _UUID, sa.ForeignKey("accounts.id", ondelete="SET NULL")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_mirrors_tenant_name"),
        # Dropped once tenants can be linked and a mirror may cross them.
        sa.CheckConstraint("dest_tenant_id = tenant_id", name="ck_mirrors_same_tenant"),
        sa.CheckConstraint("format IN ('qa')", name="ck_mirrors_format"),
        sa.CheckConstraint("dest_kind IN ('channel', 'thread')", name="ck_mirrors_dest_kind"),
        sa.CheckConstraint(
            "consent_basis IS NULL OR consent_basis IN ('attested')",
            name="ck_mirrors_consent_basis",
        ),
        sa.CheckConstraint("status IN ('active', 'paused')", name="ck_mirrors_status"),
    )
    op.create_index(
        "ix_mirrors_active", "mirrors", ["tenant_id"], postgresql_where=sa.text("status = 'active'")
    )
    op.create_table(
        "mirror_deliveries",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "mirror_id", _UUID, sa.ForeignKey("mirrors.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("source_session_key", sa.Text(), nullable=False),
        sa.Column("source_account_id", _UUID, sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("note", sa.Text()),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("dest_thread_id", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("mirror_id", "event_id", name="uq_mirror_deliveries_event"),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'delivered', 'skipped')",
            name="ck_mirror_deliveries_status",
        ),
    )
    op.create_index(
        "ix_mirror_deliveries_pending",
        "mirror_deliveries",
        ["created_at"],
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )
    op.create_index(
        "ix_mirror_deliveries_session",
        "mirror_deliveries",
        ["mirror_id", "source_session_key", "created_at"],
    )
    op.create_index("ix_mirror_deliveries_account", "mirror_deliveries", ["source_account_id"])
    op.create_index(
        "ix_mirror_deliveries_thread",
        "mirror_deliveries",
        ["dest_thread_id"],
        postgresql_where=sa.text("dest_thread_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Pending deliveries and every mirror's recorded consent are lost.
    op.drop_table("mirror_deliveries")
    op.drop_table("mirrors")
