"""agent_drafts: web edits to an agent, and the versions they applied.

Adds `agent_drafts` (one unsaved draft per person per agent, cascading from the
account, with the config it began from) and `agent_versions` (each config a web apply
or revert wrote, and the config a draft began from; the author is kept as SET NULL).

Downgrading drops every draft and the recorded version history.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0151_agent_drafts"
down_revision: str | None = "0150_usage_agent_allowances"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "agent_drafts",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("base_config", JSONB(), nullable=False),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "account_id", name="pk_agent_drafts"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_agent_drafts_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE", name="fk_agent_drafts_accounts"
        ),
    )
    op.create_index("ix_agent_drafts_account", "agent_drafts", ["account_id"])
    op.create_table(
        "agent_versions",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("changes", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "version", name="pk_agent_versions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_agent_versions_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_agent_versions_accounts",
        ),
    )
    op.create_index("ix_agent_versions_account", "agent_versions", ["account_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("agent_versions")
    op.drop_table("agent_drafts")
