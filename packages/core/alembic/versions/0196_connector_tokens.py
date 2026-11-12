"""connector_tokens: OAuth connector tokens Daimon itself holds and refreshes.

A connector sign-in on an agent that does not run on Managed Agents stores its
tokens here, Fernet-encrypted, so Daimon is the one refresh owner for that
grant. Rows are personal (an account) or agent-wide (no account). Downgrading
drops the table and every stored token; those people sign in again.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0196_connector_tokens"
down_revision: str | None = "0195_agent_record_versions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "connector_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_url", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("token_endpoint", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("token_endpoint_auth_method", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_owner", sa.Text(), nullable=False),
        sa.Column("refresh_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "refresh_owner IN ('ma', 'daimon')", name="ck_connector_tokens_refresh_owner"
        ),
    )
    op.create_index(
        "uq_connector_tokens_personal",
        "connector_tokens",
        ["tenant_id", "account_id", "agent_id", "server_url"],
        unique=True,
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )
    op.create_index(
        "uq_connector_tokens_agent_wide",
        "connector_tokens",
        ["tenant_id", "agent_id", "server_url"],
        unique=True,
        postgresql_where=sa.text("account_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("connector_tokens")
